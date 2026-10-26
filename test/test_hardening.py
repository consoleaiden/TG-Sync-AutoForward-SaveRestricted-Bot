"""稳定性加固的离线测试（不连 Telegram）。

覆盖：
* 目标为自己的频道时可选的 UserBot 优先（顺序重排）
* 单条策略超时后继续降级（防死连接把任务挂死）
* 媒体引用过期 → 重取消息刷新 → 重试成功
* 下载预取命中/清理
* 已映射消息的幂等跳过
* 失败清单登记与一键补发
* 完成消息里的完整性/覆盖率报告
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import errors

from core.forwarder import ST_UB_FORWARD, Forwarder
from core.syncer import Syncer
from db import models
from db.database import Database
from test.test_forward_strategy import (
    SRC,
    TGT,
    FakeClient,
    FakeEntity,
    FakeMessage,
    make_forwarder,
)

MSG_ID = 42


class SlowBot(FakeClient):
    """策略1 永不返回（模拟半开连接/TCP 假死）。"""

    async def forward_messages(self, *args, **kwargs):
        await asyncio.sleep(30)


class TestStrategyOrderAndTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_prefer_userbot_switch_reorders_chain(self):
        bot = FakeClient("bot", resolvable=True, entity=FakeEntity())
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub,
                            transfer={"prefer_userbot_on_own_target": True})

        result = await fw.forward_message(SRC, MSG_ID, TGT, mode="copy",
                                          caller="syncer")

        self.assertIsNotNone(result)
        self.assertEqual(len(ub.forward_calls), 1, "开关打开时应由 UserBot 先转发")
        self.assertEqual(bot.forward_calls, [], "Bot 不应被先调用")
        self.assertEqual(fw.strategy_counts[ST_UB_FORWARD], 1)

    async def test_default_keeps_bot_first(self):
        bot = FakeClient("bot", resolvable=True, entity=FakeEntity())
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub)

        await fw.forward_message(SRC, MSG_ID, TGT, mode="copy", caller="syncer")

        self.assertEqual(len(bot.forward_calls), 1, "默认仍由 Bot 直发（更快）")
        self.assertEqual(ub.forward_calls, [])

    async def test_strategy_timeout_falls_through(self):
        bot = SlowBot("bot", resolvable=True, entity=FakeEntity())
        ub = FakeClient("ub", resolvable=True, entity=FakeEntity())
        fw = make_forwarder(bot=bot, userbot=ub,
                            transfer={"strategy_timeout": 1})

        result = await fw.forward_message(SRC, MSG_ID, TGT, mode="copy",
                                          caller="syncer")

        self.assertIsNotNone(result, "策略1 超时后应由策略2 接手")
        self.assertEqual(fw.strategy_timeouts, 1)
        self.assertEqual(len(ub.forward_calls), 1)


class TestFreshFileReference(unittest.IsolatedAsyncioTestCase):
    async def test_expired_reference_refetches_and_succeeds(self):
        fw = make_forwarder()
        ub = FakeClient("ub", messages={MSG_ID: FakeMessage(MSG_ID)})
        attempts = {"n": 0}

        async def action(msg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise errors.FileReferenceExpiredError(request=None)
            return msg.id

        result = await fw._with_fresh_ref(ub, SRC, MSG_ID, action, label="test")

        self.assertEqual(result, MSG_ID, "刷新引用后应成功")
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(fw.ref_retries, 1)

    async def test_retries_exhausted_raises(self):
        fw = make_forwarder(transfer={"file_ref_retries": 1})
        ub = FakeClient("ub", messages={MSG_ID: FakeMessage(MSG_ID)})

        async def always_expired(msg):
            raise errors.FileReferenceExpiredError(request=None)

        with self.assertRaises(errors.FileReferenceExpiredError):
            await fw._with_fresh_ref(ub, SRC, MSG_ID, always_expired, label="test")
        self.assertEqual(fw.ref_retries, 1, "只应重试配置的次数")


class TestPrefetch(unittest.IsolatedAsyncioTestCase):
    async def test_take_prefetch_returns_cached_path(self):
        fw = make_forwarder()
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "media.bin")
        with open(path, "wb") as f:
            f.write(b"data")
        fw._prefetch[(SRC, MSG_ID)] = {"task": None, "path": path, "tmpdir": tmpdir}

        got = await fw._take_prefetch(SRC, MSG_ID)

        self.assertEqual(got, (path, tmpdir))
        self.assertEqual(fw._prefetch, {}, "取走后不应残留")

    async def test_cleanup_prefetch_removes_tmpdir(self):
        fw = make_forwarder()
        tmpdir = tempfile.mkdtemp()
        fw._prefetch[(SRC, MSG_ID)] = {"task": None, "path": None, "tmpdir": tmpdir}

        fw.cleanup_prefetch()

        self.assertFalse(os.path.exists(tmpdir), "临时目录应被清掉")
        self.assertEqual(fw._prefetch, {})


class FakeUserbot:
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

    def iter_messages(self, chat_id, reverse=False, offset_id=0, reply_to=None, **kw):
        async def gen():
            for m in self.msgs:
                if m.id > offset_id:
                    yield m
        return gen()


class RecordingBot:
    def __init__(self):
        self.texts = []

    async def send_message(self, chat, text, **kwargs):
        self.texts.append(text)
        return SimpleNamespace(id=len(self.texts))

    async def edit_message(self, chat, mid, text, **kwargs):
        self.texts.append(text)
        return SimpleNamespace(id=mid)

    async def get_entity(self, chat_id):
        return SimpleNamespace(restricted=False, restriction_reason=[])


class StubForwarder:
    """可指定若干条失败的转发桩。"""

    def __init__(self, syncer=None, task_id=None, fail_ids=(), cancel_after=None):
        self.syncer = syncer
        self.task_id = task_id
        self.fail_ids = set(fail_ids)
        self.cancel_after = cancel_after
        self.calls = []
        self._seq = 1000

    async def forward_message(self, src_chat, msg_id, tgt_chat, mode,
                              topic=None, caller=None, userbot=None):
        self.calls.append(msg_id)
        if msg_id in self.fail_ids:
            return None
        self._seq += 1
        if (self.cancel_after is not None and self.syncer
                and len(self.calls) >= self.cancel_after):
            self.syncer._cancel_flags[self.task_id] = True
        return self._seq

    async def forward_album(self, src_chat, ids, tgt_chat, mode,
                            topic=None, caller=None, userbot=None):
        self.calls.extend(ids)
        out = []
        for i in ids:
            if i in self.fail_ids:
                continue
            self._seq += 1
            out.append(self._seq)
        return out

    def should_prefetch(self):
        return False

    async def prefetch_media(self, *args, **kwargs):
        return None

    def cleanup_prefetch(self):
        return None

    def strategy_summary(self):
        return ""

    def diagnostics_summary(self):
        return ""


class SyncerHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "harden.db"))
        await self.db.init()
        self.msgs = [SimpleNamespace(id=i, grouped_id=None, text=f"m{i}", media=None)
                     for i in range(1, 11)]
        self.userbot = FakeUserbot(self.msgs)
        self.bot = RecordingBot()
        self.config = {
            "clone": {"batch_size": 4, "progress_notify_every": 5,
                      "stall_alert_seconds": 0},
            "rate_limit": {"forward_interval": [0, 0], "batch_pause_every": 999,
                           "batch_pause_time": [0, 0]},
        }
        self.syncer = Syncer(bot=self.bot, userbot=self.userbot,
                             db=self.db, config=self.config)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_already_mapped_messages_are_skipped(self):
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        for i in range(1, 6):
            await models.save_message_map(self.db, task_id, i, 900 + i)
        stub = StubForwarder(self.syncer, task_id)
        self.syncer.forwarder = stub

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        self.assertEqual(stub.calls, list(range(6, 11)), "前 5 条已映射，不应重发")
        t = await models.get_task(self.db, task_id)
        # done_count 只统计「本次真正发出」的条数，已映射的计入 skip，
        # 避免同一批消息在 done 与 message_map 里被重复计数。
        self.assertEqual(t["done_count"], 5)
        self.assertEqual(t["skip_count"], 5, "已映射的计入跳过")
        self.assertEqual(t["fail_count"], 0)
        self.assertEqual(await models.count_message_map(self.db, task_id), 10)

    async def test_failures_recorded_then_retried(self):
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        stub = StubForwarder(self.syncer, task_id, fail_ids={3, 4})
        self.syncer.forwarder = stub

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     userbot=self.userbot)

        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["fail_count"], 2)
        failed = await models.list_failed_messages(self.db, task_id)
        self.assertEqual([r["source_msg_id"] for r in failed], [3, 4])

        # 第二次不再失败，补发应把它们补上并清空失败清单
        stub.fail_ids.clear()
        ok, still = await self.syncer.retry_failed(task_id)

        self.assertEqual((ok, still), (2, 0))
        self.assertEqual(await models.count_failed_messages(self.db, task_id), 0)
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["done_count"], 10)
        self.assertEqual(t["last_synced_msg_id"], 10, "补发不应改动续跑游标")

    async def test_completion_message_reports_coverage(self):
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        stub = StubForwarder(self.syncer, task_id, fail_ids={7})
        self.syncer.forwarder = stub

        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)

        final = self.bot.texts[-1]
        self.assertIn("克隆完成", final)
        self.assertIn("完整性", final, "完成后必须报告覆盖率")
        self.assertIn("覆盖率 90.0%", final, "10 条里成功 9 条")
        self.assertIn("缺口 1 条", final)
        self.assertIn("/retryfail", final)

    async def test_completion_progress_reaches_100_percent(self):
        """进度条口径 = 已处理（成功+失败+跳过），处理完必须显示 100%。"""
        # 场景 1：一半已映射（跳过）+ 一半成功
        task_id = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        for i in range(1, 6):
            await models.save_message_map(self.db, task_id, i, 900 + i)
        stub = StubForwarder(self.syncer, task_id)
        self.syncer.forwarder = stub
        await self.syncer.start_sync(task_id, -1001, -1002, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)
        final = self.bot.texts[-1]
        self.assertIn("100.0%", final,
                      "全部处理完（成功+跳过=总数）进度应为 100%")

        # 场景 2：有失败条目（未补发）也属「已处理」，同样应到 100%
        task_id2 = await models.create_task(self.db, "sync", -1001, -1002, "copy")
        stub2 = StubForwarder(self.syncer, task_id2, fail_ids={7})
        self.syncer.forwarder = stub2
        await self.syncer.start_sync(task_id2, -1001, -1002, "copy",
                                     notify_chat_id=-999,
                                     userbot=self.userbot)
        final2 = self.bot.texts[-1]
        self.assertIn("100.0%", final2, "失败也算已处理，进度应到 100%")
        self.assertIn("缺口 1 条", final2)


if __name__ == "__main__":
    unittest.main()
