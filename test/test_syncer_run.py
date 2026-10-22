"""Syncer.start_sync 的离线运行测试：完整跑完 / 中途暂停 / 从游标续传。

用假 UserBot + 假 Forwarder，不连 Telegram。重点验证：
* 从头跑到尾时计数与游标正确；
* 中途取消后状态为 paused 且游标停在最后成功的一条；
* 续传从游标之后继续，不重复、不遗漏。
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.syncer import Syncer
from db import models
from db.database import Database


class FakeUserbot:
    """提供 Syncer 用到的三个只读接口。"""

    def __init__(self, msgs):
        self.msgs = msgs

    async def get_messages(self, chat_id, **kwargs):
        if "offset_date" in kwargs:
            return SimpleNamespace(id=0)
        if "ids" in kwargs:
            wanted = set(kwargs["ids"])
            return [m for m in self.msgs if m.id in wanted]
        return SimpleNamespace(total=len(self.msgs))

    async def get_entity(self, chat_id):
        return SimpleNamespace(restricted=False, restriction_reason=[])

    def iter_messages(self, chat_id, reverse=False, offset_id=0, reply_to=None):
        async def gen():
            for m in self.msgs:
                if m.id > offset_id:
                    yield m
        return gen()


class StubForwarder:
    """记录被转发的源消息 ID，可配置在第 N 条后触发取消。"""

    def __init__(self, syncer=None, task_id=None, cancel_after=None):
        self.syncer = syncer
        self.task_id = task_id
        self.cancel_after = cancel_after
        self.calls = []
        self._seq = 1000

    async def forward_message(self, src_chat, msg_id, tgt_chat, mode,
                              topic=None, caller=None, userbot=None):
        self.calls.append(msg_id)
        self._seq += 1
        if (self.cancel_after is not None and self.syncer
                and len(self.calls) >= self.cancel_after):
            self.syncer._cancel_flags[self.task_id] = True
        return self._seq

    async def forward_album(self, src_chat, ids, tgt_chat, mode,
                            topic=None, caller=None, userbot=None):
        self.calls.extend(ids)
        out = []
        for _ in ids:
            self._seq += 1
            out.append(self._seq)
        if (self.cancel_after is not None and self.syncer
                and len(self.calls) >= self.cancel_after):
            self.syncer._cancel_flags[self.task_id] = True
        return out


class SyncerRunTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "run.db"))
        await self.db.init()
        self.msgs = [SimpleNamespace(id=i, grouped_id=None, text=f"m{i}", media=None)
                     for i in range(1, 26)]
        self.userbot = FakeUserbot(self.msgs)
        self.config = {
            "clone": {"batch_size": 4, "progress_notify_every": 5},
            "rate_limit": {"forward_interval": [0, 0], "batch_pause_every": 999,
                           "batch_pause_time": [0, 0]},
        }
        self.syncer = Syncer(bot=SimpleNamespace(), userbot=self.userbot,
                             db=self.db, config=self.config)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _use_forwarder(self, task_id, cancel_after=None):
        stub = StubForwarder(self.syncer, task_id, cancel_after)
        self.syncer.forwarder = stub
        return stub

    async def test_full_clone_from_head(self):
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        fwd = self._use_forwarder(task_id)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["done_count"], 25)
        self.assertEqual(t["last_synced_msg_id"], 25)
        self.assertEqual(t["total_count"], 25)
        self.assertEqual(fwd.calls, list(range(1, 26)))
        self.assertIsNotNone(t["finished_at"])

    async def test_pause_then_resume_without_duplicates(self):
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        fwd = self._use_forwarder(task_id, cancel_after=10)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["status"], "paused", "取消后应标记为 paused")
        self.assertEqual(t["last_synced_msg_id"], 10, "游标应停在最后成功的一条")
        self.assertEqual(t["done_count"], 10)
        first_run = list(fwd.calls)
        self.assertEqual(first_run, list(range(1, 11)))

        # 续传：不再取消
        fwd2 = self._use_forwarder(task_id)
        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["done_count"], 25)
        self.assertEqual(fwd2.calls, list(range(11, 26)),
                         "续传必须从游标之后开始，不能重复")
        self.assertFalse(set(first_run) & set(fwd2.calls), "出现重复转发")

    async def test_start_from_msg_id(self):
        task_id = await models.create_task(
            self.db, "sync", -1001, -1002, "copy", start_msg_id=11)
        fwd = self._use_forwarder(task_id)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        self.assertEqual(fwd.calls, list(range(11, 26)),
                         "从指定 ID 开始应包含该条")
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 15)
        self.assertEqual(t["status"], "completed")

    async def test_start_from_date_uses_boundary(self):
        class DateUserbot(FakeUserbot):
            async def get_messages(self, chat_id, **kwargs):
                if "offset_date" in kwargs:
                    return SimpleNamespace(id=7)     # 边界：7 及之前跳过
                if "ids" in kwargs:
                    return [m for m in self.msgs if m.id in set(kwargs["ids"])]
                return SimpleNamespace(total=len(self.msgs))

        task_id = await models.create_task(
            self.db, "sync", -1001, -1002, "copy",
            start_date="2024-01-31T00:00:00+08:00")
        fwd = self._use_forwarder(task_id)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=DateUserbot(self.msgs))

        self.assertEqual(fwd.calls, list(range(8, 26)))
        # 剩余 18 条
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 18)

    async def test_skips_empty_messages(self):
        self.msgs[4] = SimpleNamespace(id=5, grouped_id=None, text=None, media=None)
        self.userbot.msgs = self.msgs
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        fwd = self._use_forwarder(task_id)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        self.assertNotIn(5, fwd.calls)
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 24)
        self.assertEqual(t["skip_count"], 1)
        self.assertEqual(t["last_synced_msg_id"], 25)

    async def test_albums_forwarded_as_group(self):
        self.msgs = [SimpleNamespace(id=1, grouped_id=None, text="a", media=None),
                     SimpleNamespace(id=2, grouped_id=77, text="b", media=object()),
                     SimpleNamespace(id=3, grouped_id=77, text="c", media=object()),
                     SimpleNamespace(id=4, grouped_id=77, text="d", media=object())]
        self.userbot.msgs = self.msgs
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        fwd = self._use_forwarder(task_id)

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        self.assertEqual(fwd.calls, [1, 2, 3, 4])
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 4)
        # 相册三张要各自建立映射
        cursor = await self.db.db.execute(
            "SELECT COUNT(*) AS c FROM message_map WHERE task_id=?", (task_id,))
        self.assertEqual((await cursor.fetchone())["c"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
