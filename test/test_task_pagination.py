"""任务列表分页（每页 5 条）与「克隆完成自动转监听」的数据/渲染测试。

覆盖：
- 数据层：总数统计（含按状态计运行中）、limit/offset 分页切片、排序稳定；
- 克隆列表：12 个任务分 3 页，首页/末页按钮显隐、越界页码收敛到最后一页；
- 监听列表：7 个监听分 2 页，克隆转来的监听带「来自克隆任务 #ID」标记；
- 受限克隆：完成钩子被调用，钩子抛错不影响收尾。
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.menu import PAGE_SIZE, MenuController
from core.restricted_syncer import RestrictedSyncer
from db import models
from db.database import Database


class FakeEvent:
    def __init__(self):
        self.edited = []
        self.sent = []

    async def edit(self, text, buttons=None):
        self.edited.append((text, buttons))

    async def respond(self, text, buttons=None):
        self.sent.append((text, buttons))


def button_labels(buttons):
    return [getattr(b, "text", "") for row in (buttons or []) for b in row]


def button_datas(buttons):
    """取回调数据：Telethon 1.45 起在 button.type.data（bytes）。"""
    out = []
    for row in (buttons or []):
        for b in row:
            kind = getattr(b, "type", None)
            data = getattr(kind, "data", b"") if kind is not None else b""
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "page.db"))
        await self.db.init()

        async def _ok(*a, **kw):
            return None

        async def _fmt(chat_id, topic_id=None, prefer_bot=False):
            return f"会话 {chat_id}"

        self.menu = MenuController(
            None, db=self.db, config={}, is_admin=_ok,
            format_display=_fmt, resolve_source=_ok, resolve_target=_ok,
            create_task=_ok, stop_task=_ok, resume_task=_ok)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    async def _make_clones(self, n: int, *, running: int = 0):
        ids = []
        for i in range(n):
            tid = await models.create_task(
                self.db, "sync", -1000 - i, -2000 - i,
                source_title=f"源频道{i}", target_title=f"目标群{i}")
            if i >= running:
                await models.update_task_status(self.db, tid, "completed")
            ids.append(tid)
        return ids


class CountAndPageDataTest(_Base):
    async def test_counts_and_slices(self):
        await self._make_clones(12, running=2)
        self.assertEqual(
            await models.count_tasks_for_list(self.db, models.CLONE_TASK_TYPES), 12)
        self.assertEqual(
            await models.count_tasks_for_list(
                self.db, models.CLONE_TASK_TYPES, statuses=("running",)), 2)

        page1 = await models.get_tasks_for_list(
            self.db, models.CLONE_TASK_TYPES, limit=5, offset=0)
        page2 = await models.get_tasks_for_list(
            self.db, models.CLONE_TASK_TYPES, limit=5, offset=5)
        page3 = await models.get_tasks_for_list(
            self.db, models.CLONE_TASK_TYPES, limit=5, offset=10)
        self.assertEqual([len(page1), len(page2), len(page3)], [5, 5, 2])
        # 三页拼起来不重不漏
        ids = [t["id"] for t in page1 + page2 + page3]
        self.assertEqual(len(set(ids)), 12)
        # 运行中排最前
        self.assertTrue(all(t["status"] == "running" for t in page1[:2]))

    async def test_deleted_excluded_from_count(self):
        ids = await self._make_clones(3)
        await models.update_task_status(self.db, ids[0], "deleted")
        self.assertEqual(
            await models.count_tasks_for_list(self.db, models.CLONE_TASK_TYPES), 2)

    async def test_monitor_count_and_page(self):
        for i in range(7):
            await models.create_task(
                self.db, "monitor", -3000 - i, -4000 - i,
                source_title=f"源{i}", target_title=f"目标{i}")
        self.assertEqual(await models.count_tasks_by_type(self.db, "monitor"), 7)
        p1 = await models.get_tasks_by_type(self.db, "monitor", limit=5, offset=0)
        p2 = await models.get_tasks_by_type(self.db, "monitor", limit=5, offset=5)
        self.assertEqual((len(p1), len(p2)), (5, 2))


class RenderTasksPagingTest(_Base):
    async def test_first_page_has_next_only(self):
        await self._make_clones(12)
        ev = FakeEvent()
        await self.menu.render_tasks(ev, page=0, edit=False)
        text, buttons = ev.sent[-1]
        labels = button_labels(buttons)
        datas = button_datas(buttons)
        self.assertIn("共 12 个", text)
        self.assertIn("第 1/3 页", text)
        self.assertIn("下一页 ▶", labels)
        self.assertNotIn("◀ 上一页", labels)
        self.assertIn("m:tasks:p1", datas)
        # 每页 5 条任务按钮（data 为 m:task:<id>）
        self.assertEqual(
            len([d for d in datas if d.startswith("m:task:")]), PAGE_SIZE)

    async def test_middle_page_has_both(self):
        await self._make_clones(12)
        ev = FakeEvent()
        await self.menu.render_tasks(ev, page=1, edit=False)
        text, buttons = ev.sent[-1]
        labels = button_labels(buttons)
        self.assertIn("第 2/3 页", text)
        self.assertIn("◀ 上一页", labels)
        self.assertIn("下一页 ▶", labels)

    async def test_last_page_and_clamp(self):
        await self._make_clones(12)
        ev = FakeEvent()
        await self.menu.render_tasks(ev, page=2, edit=False)
        text, buttons = ev.sent[-1]
        self.assertIn("第 3/3 页", text)
        self.assertNotIn("下一页 ▶", button_labels(buttons))
        self.assertEqual(
            len([d for d in button_datas(buttons) if d.startswith("m:task:")]), 2)

        # 越界页码 → 收敛到最后一页（不会空白）
        ev2 = FakeEvent()
        await self.menu.render_tasks(ev2, page=99, edit=False)
        self.assertIn("第 3/3 页", ev2.sent[-1][0])

    async def test_single_page_has_no_pager(self):
        await self._make_clones(3)
        ev = FakeEvent()
        await self.menu.render_tasks(ev, page=0, edit=False)
        labels = button_labels(ev.sent[-1][1])
        self.assertNotIn("下一页 ▶", labels)
        self.assertNotIn("◀ 上一页", labels)


class RenderMonitorsPagingTest(_Base):
    async def test_monitor_paging_and_origin_mark(self):
        clone_id = await models.create_task(
            self.db, "sync", -9001, -9002, source_title="克隆源")
        await models.update_task_status(self.db, clone_id, "completed")
        # 6 个普通监听 + 1 个由克隆任务转来的监听
        for i in range(6):
            await models.create_task(
                self.db, "monitor", -3100 - i, -4100 - i,
                source_title=f"监听源{i}", target_title=f"监听目标{i}")
        await models.create_task(
            self.db, "monitor", -9001, -9002, parent_id=clone_id,
            source_title="克隆源", target_title="克隆目标")

        ev = FakeEvent()
        await self.menu.render_monitors(ev, page=0, edit=False)
        text, buttons = ev.sent[-1]
        self.assertIn("共 7 个", text)
        self.assertIn("第 1/2 页", text)
        self.assertIn("下一页 ▶", button_labels(buttons))
        # 克隆转来的监听带来源标记（按 id 倒序，最新创建的在第 1 页）
        self.assertIn("来自克隆任务", text)

        ev2 = FakeEvent()
        await self.menu.render_monitors(ev2, page=1, edit=False)
        text2, buttons2 = ev2.sent[-1]
        self.assertIn("第 2/2 页", text2)
        self.assertNotIn("下一页 ▶", button_labels(buttons2))
        self.assertEqual(
            len([d for d in button_datas(buttons2) if d.startswith("m:task:")]), 2)


class _FakeUserbot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def takeout(self):
        return self


class RestrictedCompleteHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_fire_complete_calls_hook(self):
        syncer = RestrictedSyncer.__new__(RestrictedSyncer)
        syncer.on_complete = None
        calls = []

        async def hook(task_id):
            calls.append(task_id)

        syncer.on_complete = hook
        await syncer._fire_complete(42)
        self.assertEqual(calls, [42])

    async def test_fire_complete_swallows_hook_error(self):
        syncer = RestrictedSyncer.__new__(RestrictedSyncer)

        async def boom(task_id):
            raise RuntimeError("monitor 创建失败")

        syncer.on_complete = boom
        await syncer._fire_complete(7)   # 不应抛出

    async def test_fire_complete_without_hook_is_noop(self):
        syncer = RestrictedSyncer.__new__(RestrictedSyncer)
        syncer.on_complete = None
        await syncer._fire_complete(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
