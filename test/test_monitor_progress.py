"""监控任务进度与去重测试（离线：假 client / 假 event，不联网）。

覆盖：
- find_monitor_task 的同源同目标去重（含话题区分）
- 监控转发计数（内存累加 → 批量落盘）、最近消息 ID
- 菜单「监控任务」总览与详情页展示已转发条数
- 向导确认「已存在相同监控」时不重复创建
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.menu import MenuController
from core.monitor import MonitorManager
from db import models
from db.database import Database


class FakeEvent:
    def __init__(self, chat_id=-100222, sender_id=7):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = ""
        self.edited = []
        self.sent = []
        self.answers = []

    async def edit(self, text, buttons=None):
        self.edited.append((text, buttons))

    async def respond(self, text, buttons=None):
        self.sent.append((text, buttons))

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def reply(self, text, **kwargs):
        self.sent.append((text, kwargs.get("buttons")))

    @property
    def last_text(self):
        return self.edited[-1][0] if self.edited else ""


def button_labels(buttons):
    return [getattr(b, "text", "") for row in (buttons or []) for b in row]


class MonitorStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "monitor.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    async def test_find_monitor_task_matches_same_route_only(self):
        task_id = await models.create_task(
            self.db, "monitor", -1001, -2002,
            source_topic_id=None, target_topic_id=None)

        found = await models.find_monitor_task(self.db, -1001, -2002, None, None)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], task_id)

        # 目标不同 / 源话题不同 → 不算重复
        self.assertIsNone(await models.find_monitor_task(self.db, -1001, -9999, None, None))
        self.assertIsNone(await models.find_monitor_task(self.db, -1001, -2002, 3, None))

        # 已暂停的相同任务同样算重复（避免暂停后重建出现双份）
        await models.update_task_status(self.db, task_id, "paused")
        found = await models.find_monitor_task(self.db, -1001, -2002, None, None)
        self.assertEqual(found["id"], task_id)

        # 删除后允许重新创建
        await models.update_task_status(self.db, task_id, "deleted")
        self.assertIsNone(await models.find_monitor_task(self.db, -1001, -2002, None, None))


class MonitorStatsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "stats.db"))
        await self.db.init()
        self.task_id = await models.create_task(self.db, "monitor", -1001, -2002)

        self.mgr = MonitorManager(MagicMock(), MagicMock(), self.db, {})
        self.mgr.forwarder = MagicMock()
        self.mgr.forwarder.detect_restriction = AsyncMock(return_value=(False, None))
        self.mgr.forwarder.forward_message = AsyncMock(return_value=555)
        self.mgr.forwarder.send_fail_marker = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    async def test_counters_flushed_to_db(self):
        self.mgr.bump_stats(self.task_id, done=2, msg_id=120)
        self.mgr.bump_stats(self.task_id, fail=1, skip=1, msg_id=121)
        await self.mgr.flush_stats(self.task_id)

        t = await models.get_task(self.db, self.task_id)
        self.assertEqual(t["done_count"], 2)
        self.assertEqual(t["fail_count"], 1)
        self.assertEqual(t["skip_count"], 1)
        self.assertEqual(t["last_synced_msg_id"], 121)

        # 落盘后计数清零，不会重复累加
        await self.mgr.flush_stats(self.task_id)
        t = await models.get_task(self.db, self.task_id)
        self.assertEqual(t["done_count"], 2)

    async def test_handler_counts_forwarded_message(self):
        await self.mgr.start_monitor(self.task_id, -1001, -2002, "copy")

        handler = self.mgr._handlers[self.task_id][0]
        event = SimpleNamespace(
            message=SimpleNamespace(id=42, grouped_id=None, reply_to=None))
        await handler(event)
        await self.mgr.flush_stats(self.task_id)

        t = await models.get_task(self.db, self.task_id)
        self.assertEqual(t["done_count"], 1)
        self.assertEqual(t["last_synced_msg_id"], 42)

        await self.mgr.stop_monitor(self.task_id)
        self.assertFalse(self.mgr.is_running(self.task_id))

    async def test_get_live_stats_merges_pending(self):
        self.mgr.bump_stats(self.task_id, done=3, msg_id=90)
        stats = await self.mgr.get_live_stats(self.task_id)
        self.assertEqual(stats["done"], 3)
        self.assertEqual(stats["last_msg_id"], 90)


class MonitorMenuTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "menu.db"))
        await self.db.init()
        self.created = []

        async def _format_display(chat_id, topic_id=None, prefer_bot=False):
            return f"会话 {chat_id}"

        async def _is_admin(event):
            return True

        async def _create_task(payload, event):
            self.created.append(payload)
            return 77, "exists"

        async def _ok(*a, **kw):
            return None

        self.menu = MenuController(
            None, db=self.db, config={}, is_admin=_is_admin,
            format_display=_format_display,
            resolve_source=_ok, resolve_target=_ok,
            create_task=_create_task, stop_task=_ok,
            resume_task=lambda task_id: _ok())

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    async def test_monitors_view_lists_sources_and_counts(self):
        task_id = await models.create_task(
            self.db, "monitor", -1001, -2002,
            source_title="源频道A", target_title="目标群B")
        await models.update_progress(self.db, task_id, done_delta=15,
                                     last_msg_id=888)

        ev = FakeEvent()
        await self.menu.render_monitors(ev, edit=False)

        text = ev.sent[-1][0]
        self.assertIn("监听任务", text)
        self.assertIn("源频道A", text)
        self.assertIn("目标群B", text)
        self.assertIn("已转发 15", text)
        self.assertIn("msg_id=888", text)
        self.assertIn(f"#{task_id}", " ".join(button_labels(ev.sent[-1][1])))

    async def test_monitor_detail_shows_forwarded_count(self):
        task_id = await models.create_task(
            self.db, "monitor", -1001, -2002,
            source_title="源频道A", target_title="目标群B")
        await models.update_progress(self.db, task_id, done_delta=7, fail_delta=1,
                                     last_msg_id=321)

        ev = FakeEvent()
        await self.menu.render_task_detail(ev, task_id, edit=False)
        text = ev.sent[-1][0]
        self.assertIn("监听范围", text)
        self.assertIn("已转发 7 条", text)
        self.assertIn("msg_id=321", text)

    async def test_confirm_duplicate_monitor_does_not_claim_created(self):
        ev = FakeEvent()
        wiz = self.menu._start_wizard(ev, "monitor")
        wiz.data["source"] = {"chat_id": -1001, "title": "源频道A"}
        wiz.data["target"] = {"chat_id": -2002, "title": "目标群B"}
        wiz.data["mode"] = "copy"
        wiz.step = "confirm"

        await self.menu._on_confirm(ev)

        self.assertIn("已存在", ev.last_text)
        self.assertNotIn("已创建，开始监听", ev.last_text)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["kind"], "monitor")
