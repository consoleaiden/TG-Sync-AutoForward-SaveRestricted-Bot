"""克隆任务持久保留 / 持续监听 / 新命令族的离线测试。

不连 Telegram：客户端、事件、转发器全部用假对象。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.tl import types

from bot.handlers import (
    describe_forwarded_source,
    latest_source_msg_id,
    parse_clone_args,
    register_handlers,
    strip_command,
)
from bot.menu import MenuController
from core.monitor import MonitorManager
from core.syncer import Syncer
from db import models
from db.database import Database


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------

class FakeBot:
    """只实现 on() 装饰器，记录注册的事件处理器。"""

    def __init__(self):
        self.handlers = []
        self.sent = []

    def on(self, event):
        def deco(fn):
            self.handlers.append((event, fn))
            return fn
        return deco

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=1)


class FakeEvent:
    def __init__(self, text="", chat_id=-100222, sender_id=7, message=None):
        self.raw_text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = message
        self.replies = []
        self.edits = []
        self.answers = []
        self.buttons = None
        self.is_private = False
        self.is_group = True
        self.is_channel = False

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def respond(self, text, **kwargs):
        self.replies.append(text)

    async def edit(self, text, buttons=None, **kwargs):
        self.edits.append(text)
        self.buttons = buttons

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def get_sender(self):
        return SimpleNamespace(id=self.sender_id)


class RangeClient:
    """记录 iter_messages / get_messages 的入参，用于验证 end_msg_id 边界。"""

    def __init__(self, msgs):
        self.msgs = msgs
        self.calls = []

    async def get_messages(self, chat_id, **kwargs):
        self.calls.append(("get_messages", kwargs))
        return SimpleNamespace(total=len(self.msgs))

    async def get_entity(self, chat_id):
        return SimpleNamespace(restricted=False, restriction_reason=[])

    def iter_messages(self, chat_id, **kwargs):
        self.calls.append(("iter_messages", kwargs))
        max_id = kwargs.get("max_id") or 0
        offset = kwargs.get("offset_id") or 0

        async def gen():
            for m in self.msgs:
                if m.id <= offset:
                    continue
                if max_id and m.id >= max_id:
                    continue
                yield m
        return gen()


# ---------------------------------------------------------------------------
# 命令参数解析
# ---------------------------------------------------------------------------

class TestCommandArgs(unittest.TestCase):
    def test_range_then_link(self):
        start, end, rest, desc = parse_clone_args("100-1100 https://t.me/abc")
        self.assertEqual((start, end), (100, 1100))
        self.assertEqual(rest, "https://t.me/abc")
        self.assertIn("100", desc)

    def test_range_supports_chinese_and_tilde(self):
        for text in ("100~1100 https://t.me/abc", "100 到 1100 https://t.me/abc",
                     "100 至 1100 https://t.me/abc", "100—1100 https://t.me/abc"):
            start, end, rest, _ = parse_clone_args(text)
            self.assertEqual((start, end), (100, 1100), text)
            self.assertTrue(rest.startswith("https://t.me/"))

    def test_reversed_range_is_normalised(self):
        start, end, _, _ = parse_clone_args("1100-100 https://t.me/abc")
        self.assertEqual((start, end), (100, 1100))

    def test_no_range(self):
        start, end, rest, desc = parse_clone_args("https://t.me/abc")
        self.assertEqual((start, end, desc), (0, 0, None))
        self.assertEqual(rest, "https://t.me/abc")

    def test_strip_command(self):
        self.assertEqual(strip_command("/kl 100-1100 x", "kl"), "100-1100 x")
        self.assertEqual(strip_command("/kl@my_bot x", "kl"), "x")
        self.assertEqual(strip_command("/kljx https://t.me/a", "kljx"),
                         "https://t.me/a")

    def test_forwarded_source_public_channel(self):
        fwd = SimpleNamespace(
            fwd_from=types.MessageFwdHeader(from_id=types.PeerChannel(123456),
                                            from_name="频道A",
                                            date=datetime.now(timezone.utc)),
            forward=None)
        info = describe_forwarded_source(fwd)
        self.assertIsNotNone(info)
        self.assertEqual(info["title"], "频道A")
        self.assertIsNone(info["username"])
        self.assertTrue(info["link"].startswith("https://t.me/c/"))
        self.assertEqual(info["chat_id"], -1000000123456)

    def test_forwarded_source_with_entity(self):
        chat = SimpleNamespace(id=555, title="公开频道", username="pubchan")
        fwd = SimpleNamespace(
            fwd_from=types.MessageFwdHeader(from_id=types.PeerChannel(555),
                                          date=datetime.now(timezone.utc)),
            forward=SimpleNamespace(chat=chat))
        info = describe_forwarded_source(fwd)
        self.assertEqual(info["link"], "https://t.me/pubchan")
        self.assertEqual(info["username"], "pubchan")

    def test_plain_message_has_no_source(self):
        self.assertIsNone(describe_forwarded_source(
            SimpleNamespace(fwd_from=None, forward=None)))


# ---------------------------------------------------------------------------
# 数据层：新字段 + 持久保留
# ---------------------------------------------------------------------------

class TestTaskSchema(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tasks.db")
        self.db = Database(self.path)
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_new_columns_exist(self):
        cur = await self.db.db.execute("PRAGMA table_info(tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        for name in ("end_msg_id", "follow", "follow_task_id", "parent_id"):
            self.assertIn(name, cols)

    async def test_migration_adds_columns_to_legacy_db(self):
        legacy = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("""CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
            source_chat_id INTEGER NOT NULL, source_topic_id INTEGER,
            target_chat_id INTEGER NOT NULL, target_topic_id INTEGER,
            mode TEXT DEFAULT 'copy', status TEXT DEFAULT 'running',
            last_synced_msg_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO tasks (type, source_chat_id, target_chat_id,"
                     " last_synced_msg_id) VALUES ('sync', -1001, -1002, 42)")
        conn.commit()
        conn.close()

        db = Database(legacy)
        await db.init()
        try:
            cols = {r["name"] for r in
                    await (await db.db.execute("PRAGMA table_info(tasks)")).fetchall()}
            self.assertIn("follow", cols)
            tasks = await models.get_tasks_for_list(db, models.CLONE_TASK_TYPES)
            self.assertEqual(tasks[0]["last_synced_msg_id"], 42)
            self.assertEqual(int(tasks[0]["follow"] or 0), 0)
        finally:
            await db.close()

    async def test_create_task_stores_range_and_follow(self):
        tid = await models.create_task(
            self.db, "sync", -1001, -1002, start_msg_id=100, end_msg_id=1100,
            follow=1, parent_id=None, source_title="源A")
        t = await models.get_task(self.db, tid)
        self.assertEqual(t["start_msg_id"], 100)
        self.assertEqual(t["end_msg_id"], 1100)
        self.assertEqual(t["follow"], 1)
        self.assertEqual(t["source_title"], "源A")

    async def test_completed_clone_kept_in_list_and_findable(self):
        tid = await models.create_task(self.db, "sync", -1001, -1002)
        await models.update_progress(self.db, tid, last_msg_id=500)
        await models.update_task_status(self.db, tid, "completed")

        tasks = await models.get_tasks_for_list(self.db, models.CLONE_TASK_TYPES)
        self.assertEqual([t["id"] for t in tasks], [tid])
        found = await models.find_clone_task(self.db, -1001, -1002, None, None)
        self.assertEqual(found["id"], tid)
        self.assertEqual(found["status"], "completed")

    async def test_reopen_clears_end_and_sets_running(self):
        tid = await models.create_task(self.db, "sync", -1001, -1002,
                                       end_msg_id=1100, follow=0)
        await models.update_progress(self.db, tid, last_msg_id=1100)
        await models.update_task_status(self.db, tid, "completed")

        await models.reopen_clone_task(self.db, tid, follow=1, clear_end=True)
        t = await models.get_task(self.db, tid)
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["follow"], 1)
        self.assertEqual(t["end_msg_id"], 0)
        self.assertEqual(t["last_synced_msg_id"], 1100)

    async def test_set_follow_and_find_follow_task(self):
        clone_id = await models.create_task(self.db, "sync", -1001, -1002)
        monitor_id = await models.create_task(
            self.db, "monitor", -1001, -1002, parent_id=clone_id)
        await models.set_follow(self.db, clone_id, 1, monitor_id)

        clone = await models.get_task(self.db, clone_id)
        self.assertEqual(clone["follow"], 1)
        self.assertEqual(clone["follow_task_id"], monitor_id)
        found = await models.find_follow_task(self.db, clone)
        self.assertEqual(found["id"], monitor_id)

        await models.set_follow(self.db, clone_id, 0)
        self.assertEqual((await models.get_task(self.db, clone_id))["follow"], 0)

    async def test_list_puts_running_first(self):
        a = await models.create_task(self.db, "sync", -1001, -1002)
        b = await models.create_task(self.db, "sync", -1003, -1004)
        await models.update_task_status(self.db, a, "completed")
        tasks = await models.get_tasks_for_list(self.db, models.CLONE_TASK_TYPES)
        self.assertEqual(tasks[0]["id"], b)
        self.assertEqual(tasks[1]["id"], a)


# ---------------------------------------------------------------------------
# 克隆区间边界
# ---------------------------------------------------------------------------

class TestCloneEndBound(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "end.db"))
        await self.db.init()
        self.config = {"clone": {"batch_size": 5}, "rate_limit": {}}
        self.syncer = Syncer(None, None, self.db, self.config)
        self.msgs = [SimpleNamespace(id=i, text=f"m{i}", media=None, grouped_id=None)
                     for i in range(1, 21)]

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_build_plan_counts_only_range(self):
        client = RangeClient(self.msgs)
        task = {"start_msg_id": 5, "end_msg_id": 10}
        plan = await self.syncer.build_plan(task, client, -1001, None)
        self.assertEqual(plan.offset_id, 4)
        self.assertEqual(plan.end_msg_id, 10)
        self.assertIn("到消息 10", plan.start_desc)
        # 统计剩余时必须带上 max_id 上界（end+1）
        get_calls = [kw for name, kw in client.calls if name == "get_messages"]
        self.assertTrue(any(kw.get("max_id") == 11 for kw in get_calls), client.calls)

    async def test_iter_batches_stops_at_end(self):
        client = RangeClient(self.msgs)
        counter = {"last_id": 0, "skip": 0}
        got = []
        async for batch in self.syncer._iter_batches(
                task_id=1, client=client, chat_id=-1001, topic_id=None,
                offset_id=4, counter=counter, end_msg_id=10):
            got.extend(m.id for m in batch)
        self.assertEqual(got, [5, 6, 7, 8, 9, 10])
        iter_calls = [kw for name, kw in client.calls if name == "iter_messages"]
        self.assertEqual(iter_calls[0].get("max_id"), 11)

    async def test_no_end_means_unbounded(self):
        client = RangeClient(self.msgs)
        counter = {"last_id": 0, "skip": 0}
        got = []
        async for batch in self.syncer._iter_batches(
                task_id=1, client=client, chat_id=-1001, topic_id=None,
                offset_id=0, counter=counter, end_msg_id=0):
            got.extend(m.id for m in batch)
        self.assertEqual(got[-1], 20)
        iter_calls = [kw for name, kw in client.calls if name == "iter_messages"]
        self.assertNotIn("max_id", iter_calls[0])


# ---------------------------------------------------------------------------
# 菜单：持续监听展示与开关
# ---------------------------------------------------------------------------

async def _fmt(chat_id, topic_id=None, prefer_bot=False):
    return f"chat{chat_id}"


class TestMenuFollow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "menu.db"))
        await self.db.init()
        self.calls = []

        async def toggle(task_id):
            self.calls.append(("toggle", task_id))
            return True, "🛰 已开启持续监听（监听任务 #9）"

        async def cont(task_id):
            self.calls.append(("cont", task_id))
            return True, None

        self.menu = MenuController(
            None, db=self.db, config={},
            is_admin=lambda e: True, format_display=_fmt,
            resolve_source=None, resolve_target=None,
            create_task=None, stop_task=None, resume_task=None,
            toggle_follow=toggle, continue_task=cont)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_completed_task_shows_follow_state_and_buttons(self):
        tid = await models.create_task(self.db, "sync", -1001, -1002,
                                       follow=1, source_title="源A",
                                       target_title="目标B")
        await models.update_progress(self.db, tid, last_msg_id=300)
        await models.update_task_status(self.db, tid, "completed")

        ev = FakeEvent()
        await self.menu.render_task_detail(ev, tid)
        text = ev.edits[-1]
        self.assertIn("持续监听: 🛰 开启", text)
        labels = [b.text for row in ev.buttons for b in row]
        self.assertTrue(any("关闭持续监听" in l for l in labels), labels)
        self.assertTrue(any("继续补新消息" in l for l in labels), labels)

    async def test_dispatch_follow_toggles_and_rerenders(self):
        tid = await models.create_task(self.db, "sync", -1001, -1002)
        ev = FakeEvent()
        await self.menu._dispatch(ev, f"follow:{tid}")
        self.assertEqual(self.calls, [("toggle", tid)])
        self.assertTrue(ev.edits, "开关后应刷新详情")

    async def test_dispatch_cont_calls_continue(self):
        tid = await models.create_task(self.db, "sync", -1001, -1002)
        ev = FakeEvent()
        await self.menu._dispatch(ev, f"cont:{tid}")
        self.assertEqual(self.calls, [("cont", tid)])


# ---------------------------------------------------------------------------
# /chapd 命令闭环
# ---------------------------------------------------------------------------

class TestChapdCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "cmd.db"))
        await self.db.init()
        self.bot = FakeBot()
        self.config = {"admin_ids": [7], "allow_public_resolve": True,
                       "rate_limit": {"forward_interval": [0, 0]},
                       "clone": {"auto_resume": True}}
        self.monitor = MonitorManager(self.bot, self.bot, self.db, self.config)
        register_handlers(self.bot, self.bot, self.db, self.config, self.monitor)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    def _handler(self, name):
        for _ev, fn in self.bot.handlers:
            if fn.__name__ == name:
                return fn
        self.fail(f"未注册处理器 {name}")

    async def test_chapd_then_forward_reports_channel(self):
        on_chapd = self._handler("on_chapd")
        ev = FakeEvent(text="/chapd")
        await on_chapd(ev)
        self.assertTrue(any("转发" in r for r in ev.replies))

        hdr = types.MessageFwdHeader(from_id=types.PeerChannel(987654),
                                     from_name="目标频道",
                                     date=datetime.now(timezone.utc))
        msg = SimpleNamespace(fwd_from=hdr, forward=None)
        fwd_ev = FakeEvent(text="", message=msg)
        await self._handler("on_chapd_forward")(fwd_ev)

        text = fwd_ev.replies[-1]
        self.assertIn("目标频道", text)
        self.assertIn("-1000000987654", text)
        self.assertIn("/kl https://t.me/c/987654/1", text)


class FakeSourceClient:
    """只实现 get_messages：返回给定消息列表，或抛异常。"""

    def __init__(self, msg_id=None, *, empty=False, error=None):
        self.msg_id = msg_id
        self.empty = empty
        self.error = error
        self.calls = []

    async def get_messages(self, chat_id, limit=1, reply_to=None):
        self.calls.append((chat_id, limit, reply_to))
        if self.error:
            raise self.error
        if self.empty:
            return []
        return [SimpleNamespace(id=self.msg_id)]


class TestLatestSourceMsgId(unittest.IsolatedAsyncioTestCase):
    """`/kljx` 的"已是最新"判断依赖这个函数，判定为 None 时不能当成追平。"""

    async def test_returns_latest_id_from_first_client(self):
        a = FakeSourceClient(4321)
        b = FakeSourceClient(9999)
        self.assertEqual(await latest_source_msg_id(-100123, 3, a, b), 4321)
        self.assertEqual(a.calls, [(-100123, 1, 3)])
        self.assertEqual(b.calls, [])

    async def test_falls_back_when_client_fails(self):
        bad = FakeSourceClient(error=RuntimeError("no access"))
        good = FakeSourceClient(777)
        self.assertEqual(await latest_source_msg_id(-100123, None, bad, good), 777)
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(len(good.calls), 1)

    async def test_all_clients_fail_returns_none(self):
        bad1 = FakeSourceClient(error=RuntimeError("x"))
        bad2 = FakeSourceClient(error=RuntimeError("y"))
        self.assertIsNone(await latest_source_msg_id(-100123, None, bad1, bad2, None))

    async def test_empty_source_returns_zero(self):
        self.assertEqual(
            await latest_source_msg_id(-100123, None, FakeSourceClient(empty=True)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)