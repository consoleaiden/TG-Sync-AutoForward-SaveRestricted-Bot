"""按钮菜单向导的端到端流程测试（假 bot / 假 event，不联网）。

覆盖：新建克隆（从头 / 指定 ID / 指定时间 / 继续上次）→ 选目标 → 选模式
→ 确认，最终产出给业务层的 payload 是否正确。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.menu import MenuController
from db import models
from db.database import Database


class FakeEvent:
    def __init__(self, chat_id=-100222, sender_id=7, text=""):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.message = SimpleNamespace(id=11, reply_to=None)
        self.edited = []
        self.sent = []
        self.answers = []
        self.replies = []
        self._seq = 100

    async def edit(self, text, buttons=None):
        self.edited.append((text, buttons))

    async def respond(self, text, buttons=None):
        self._seq += 1
        self.sent.append((text, buttons))
        return SimpleNamespace(id=self._seq)

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    @property
    def last_text(self):
        if self.edited:
            return self.edited[-1][0]
        if self.sent:
            return self.sent[-1][0]
        return ""

    @property
    def last_buttons(self):
        if self.edited:
            return self.edited[-1][1]
        if self.sent:
            return self.sent[-1][1]
        return None


def labels(buttons):
    out = []
    for row in buttons or []:
        for b in row:
            out.append(getattr(b, "text", ""))
    return out


class MenuFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "menu.db"))
        await self.db.init()
        self.payloads = []
        self.resume_calls = []
        self.stopped = []
        self.source = {
            "chat_id": -100111, "topic_id": None, "title": "频道 | 私有 源频道",
            "total": 1234, "userbot": "UB",
        }
        self.target = {"chat_id": -100222, "topic_id": None, "title": "群组 | 目标群"}

        async def _format_display(chat_id, topic_id=None, prefer_bot=False):
            return f"会话 {chat_id}"

        async def _is_admin(event):
            return True

        async def _resolve_source(event, raw):
            if "bad" in raw:
                return None, "解析失败"
            return dict(self.source), None

        async def _resolve_target(event, raw):
            return dict(self.target), None

        async def _create_task(payload, event):
            self.payloads.append(payload)
            start = payload.get("start") or {}
            if start.get("kind") == "resume" and start.get("task_id"):
                return int(start["task_id"]), None
            return 900 + len(self.payloads), None

        async def _stop_task(task_id):
            self.stopped.append(task_id)

        async def _resume_task(task_id):
            self.resume_calls.append(task_id)
            return True, None

        self.menu = MenuController(
            None, db=self.db, config={}, is_admin=_is_admin,
            format_display=_format_display,
            resolve_source=_resolve_source, resolve_target=_resolve_target,
            create_task=_create_task, stop_task=_stop_task, resume_task=_resume_task)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    # -- helpers -------------------------------------------------------
    async def _to_start_step(self, ev, text="https://t.me/somechannel"):
        await self.menu.show_main_menu(ev)
        wiz = self.menu._start_wizard(ev, "clone")
        await self.menu._on_text(ev, wiz)          # src
        self.assertEqual(wiz.step, "target")
        await self.menu._on_target_choice(ev, "here")
        self.assertEqual(wiz.step, "start")
        return wiz

    # -- cases ---------------------------------------------------------

    async def test_main_menu_has_entry_buttons(self):
        ev = FakeEvent()
        await self.menu.show_main_menu(ev)
        names = labels(ev.sent[-1][1])
        self.assertTrue(any("克隆" in n for n in names), names)
        self.assertTrue(any("我的任务" in n for n in names), names)

    async def test_create_clone_from_head(self):
        ev = FakeEvent()
        wiz = await self._to_start_step(ev)
        # 起点页应能显示源与总数
        self.assertIn("1,234", ev.last_text)

        await self.menu._on_start_choice(ev, "head")
        self.assertEqual(wiz.step, "mode")
        await self.menu._dispatch(ev, "mode:copy")
        self.assertEqual(wiz.step, "confirm")
        await self.menu._on_confirm(ev)

        self.assertEqual(len(self.payloads), 1)
        p = self.payloads[0]
        self.assertEqual(p["kind"], "clone")
        self.assertEqual(p["source_chat_id"], -100111)
        self.assertEqual(p["target_chat_id"], -100222)
        self.assertEqual(p["mode"], "copy")
        self.assertEqual(p["start"]["kind"], "head")
        self.assertIn("从头开始", p["start"]["desc"])
        # 任务创建后向导状态应被清理
        self.assertFalse(self.menu.has_pending(ev.chat_id, ev.sender_id))

    async def test_create_clone_from_msg_id(self):
        ev = FakeEvent()
        wiz = await self._to_start_step(ev)
        await self.menu._on_start_choice(ev, "id")
        self.assertEqual(wiz.step, "from_id")

        bad = FakeEvent(text="abc")
        await self.menu._on_text(bad, wiz)
        self.assertIn("纯数字", bad.replies[-1])
        self.assertEqual(wiz.step, "from_id")      # 非法输入不推进

        good = FakeEvent(text="4567")
        await self.menu._on_text(good, wiz)
        self.assertEqual(wiz.step, "mode")

        await self.menu._dispatch(ev, "mode:forward")
        await self.menu._on_confirm(ev)
        p = self.payloads[-1]
        self.assertEqual(p["mode"], "forward")
        self.assertEqual(p["start"]["kind"], "id")
        self.assertEqual(p["start"]["msg_id"], 4567)
        self.assertIn("4567", p["start"]["desc"])

    async def test_create_clone_from_time(self):
        ev = FakeEvent()
        wiz = await self._to_start_step(ev)
        await self.menu._on_start_choice(ev, "time")
        self.assertEqual(wiz.step, "from_time")

        bad = FakeEvent(text="不是时间")
        await self.menu._on_text(bad, wiz)
        self.assertIn("时间格式", bad.replies[-1])

        good = FakeEvent(text="2024-01-31 12:30")
        await self.menu._on_text(good, wiz)
        await self.menu._dispatch(ev, "mode:copy")
        await self.menu._on_confirm(ev)

        p = self.payloads[-1]
        self.assertEqual(p["start"]["kind"], "time")
        self.assertTrue(p["start"]["date"].startswith("2024-01-31T12:30"))
        self.assertIn("2024-01-31 12:30", p["start"]["desc"])

    async def test_resume_option_only_when_progress_exists(self):
        ev = FakeEvent()
        wiz = await self._to_start_step(ev)
        self.assertNotIn("继续上次进度", " ".join(labels(ev.last_buttons)))

        # 造一个已有游标的任务
        task_id = await models.create_task(
            self.db, "sync", -100111, -100222, "copy")
        await models.update_progress(self.db, task_id, last_msg_id=777)
        await models.update_task_status(self.db, task_id, "paused")

        ev2 = FakeEvent()
        wiz2 = await self._to_start_step(ev2)
        self.assertIn("继续上次进度", " ".join(labels(ev2.last_buttons)))

        await self.menu._on_start_choice(ev2, "resume")
        await self.menu._dispatch(ev2, "mode:copy")
        await self.menu._on_confirm(ev2)

        p = self.payloads[-1]
        self.assertEqual(p["start"]["kind"], "resume")
        self.assertEqual(p["start"]["task_id"], task_id)
        # 续传复用原任务，不再新建
        self.assertEqual(ev2.answers[-1][0], f"任务 #{task_id} 已创建")

    async def test_monitor_flow_skips_start_step(self):
        ev = FakeEvent()
        await self.menu.show_main_menu(ev)
        wiz = self.menu._start_wizard(ev, "monitor")
        await self.menu._on_text(ev, wiz)
        self.assertEqual(wiz.step, "target")
        await self.menu._on_target_choice(ev, "here")
        self.assertEqual(wiz.step, "mode")
        await self.menu._dispatch(ev, "mode:copy")
        await self.menu._on_confirm(ev)
        p = self.payloads[-1]
        self.assertEqual(p["kind"], "monitor")
        self.assertEqual(p["start"], {})

    async def test_bad_source_keeps_step(self):
        ev = FakeEvent()
        await self.menu.show_main_menu(ev)
        wiz = self.menu._start_wizard(ev, "clone")
        bad = FakeEvent(text="https://t.me/badchannel")
        await self.menu._on_text(bad, wiz)
        self.assertEqual(wiz.step, "src")
        self.assertIn("解析失败", bad.replies[-1])

    async def test_back_and_cancel(self):
        ev = FakeEvent()
        wiz = await self._to_start_step(ev)
        await self.menu._dispatch(ev, "back")
        self.assertEqual(wiz.step, "target")
        await self.menu._dispatch(ev, "cancel")
        self.assertFalse(self.menu.has_pending(ev.chat_id, ev.sender_id))

    async def test_wizard_ttl_expiry(self):
        ev = FakeEvent()
        wiz = self.menu._start_wizard(ev, "clone")
        wiz.ts -= 10 ** 4           # 超过 WIZARD_TTL
        self.assertFalse(self.menu.has_pending(ev.chat_id, ev.sender_id))

    async def test_task_detail_renders_progress(self):
        task_id = await models.create_task(
            self.db, "sync", -100111, -100222, "copy", source_title="源频道")
        await models.update_progress(self.db, task_id, total=1000, done_delta=250,
                                    last_msg_id=321)
        ev = FakeEvent()
        await self.menu.render_task_detail(ev, task_id)
        text = ev.edited[-1][0]
        self.assertIn("25.0%", text)
        self.assertIn("321", text)
        self.assertIn("源频道", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
