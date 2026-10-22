"""克隆功能（起点/进度/续传/菜单状态机）的离线单元测试。

不连 Telegram、不连真实 bot：客户端用假对象，数据库用临时文件。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite

from bot.menu import Wizard
from core.clone_plan import (
    describe_start,
    human_count,
    parse_start_time,
    progress_bar,
    split_album_tail,
)
from core.syncer import ClonePlan, Syncer
from db import models
from db.database import Database


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------

class FakeClient:
    """只实现 get_messages，用于 build_plan 的两种查询。"""

    def __init__(self, remaining=1000, boundary_id=42):
        self.remaining = remaining
        self.boundary_id = boundary_id
        self.calls = []

    async def get_messages(self, chat_id, **kwargs):
        self.calls.append(kwargs)
        if "offset_date" in kwargs:
            return SimpleNamespace(id=self.boundary_id)
        return SimpleNamespace(total=self.remaining)

    def iter_messages(self, *args, **kwargs):
        raise AssertionError("build_plan 不应遍历消息")


class FakeIterClient:
    def __init__(self, msgs):
        self.msgs = msgs

    def iter_messages(self, *args, **kwargs):
        async def gen():
            for m in self.msgs:
                yield m
        return gen()


def msg(i, grouped_id=None, text="hi", media=None):
    return SimpleNamespace(id=i, grouped_id=grouped_id, text=text, media=media)


def make_syncer(db, config=None) -> Syncer:
    cfg = {"clone": {"batch_size": 5, "progress_notify_every": 10}, **(config or {})}
    return Syncer(bot=SimpleNamespace(), userbot=SimpleNamespace(),
                  db=db, config=cfg)


async def new_db(tmpdir: str, name: str = "test.db") -> Database:
    db = Database(os.path.join(tmpdir, name))
    await db.init()
    return db


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------

class TestClonePlanHelpers(unittest.TestCase):
    def test_parse_start_time_formats(self):
        cases = {
            "2024-01-31": (2024, 1, 31, 0, 0),
            "2024-01-31 12:30": (2024, 1, 31, 12, 30),
            "2024/01/31": (2024, 1, 31, 0, 0),
            "20240131": (2024, 1, 31, 0, 0),
            "2024-01-31T08:05:06": (2024, 1, 31, 8, 5),
        }
        for text, expected in cases.items():
            dt = parse_start_time(text)
            self.assertIsNotNone(dt, text)
            self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), expected, text)
            self.assertIsNotNone(dt.tzinfo, f"{text} 应带时区")

    def test_parse_start_time_unix(self):
        dt = parse_start_time("1700000000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2023)

    def test_parse_start_time_invalid(self):
        for bad in ("", "昨天", "2024-13-40", "12:30"):
            self.assertIsNone(parse_start_time(bad), bad)

    def test_progress_bar(self):
        bar = progress_bar(50, 100)
        self.assertIn("50.0%", bar)
        self.assertIn("(50/100)", bar)
        # 总量未知时不报百分比
        unknown = progress_bar(7, None)
        self.assertNotIn("%", unknown)
        self.assertIn("7", unknown)
        # 超额不越界
        self.assertIn("100.0%", progress_bar(120, 100))

    def test_human_count(self):
        self.assertEqual(human_count(1234567), "1,234,567")
        self.assertEqual(human_count(None), "未知")

    def test_describe_start(self):
        self.assertIn("从头开始", describe_start())
        self.assertIn("123", describe_start(start_msg_id=123))
        self.assertIn("2024-01-31", describe_start(start_date="2024-01-31"))
        resumed = describe_start(resumed=True, done=50, last_msg_id=900)
        self.assertIn("断点续传", resumed)
        self.assertIn("900", resumed)


class TestSplitAlbumTail(unittest.TestCase):
    def test_no_split_when_under_batch(self):
        buf = [msg(1), msg(2)]
        ready, rest = split_album_tail(buf, 5)
        self.assertEqual(ready, [])
        self.assertEqual(len(rest), 2)

    def test_does_not_break_album(self):
        # 1..4 普通消息，5,6,7 同相册；batch=5 时应在 4 处切开
        buf = [msg(1), msg(2), msg(3), msg(4),
               msg(5, grouped_id=99), msg(6, grouped_id=99), msg(7, grouped_id=99)]
        ready, rest = split_album_tail(buf, 5)
        self.assertEqual([m.id for m in ready], [1, 2, 3, 4])
        self.assertEqual([m.id for m in rest], [5, 6, 7])

    def test_album_larger_than_batch_is_forced(self):
        buf = [msg(i, grouped_id=7) for i in range(1, 11)]
        ready, rest = split_album_tail(buf, 3)
        self.assertEqual(len(ready), 3)
        self.assertEqual(len(rest), 7)

    def test_empty(self):
        self.assertEqual(split_album_tail([], 5), ([], []))


class TestWizardStateMachine(unittest.TestCase):
    def test_step_order_and_navigation(self):
        wiz = Wizard(kind="clone", step="src")
        self.assertEqual(wiz.order(), ("src", "target", "start", "mode", "confirm"))
        self.assertEqual(wiz.next_step(), "target")
        self.assertIsNone(wiz.prev_step())

        wiz.goto("start")
        self.assertEqual(wiz.prev_step(), "target")
        self.assertEqual(wiz.next_step(), "mode")

        wiz.goto("confirm")
        self.assertIsNone(wiz.next_step())

    def test_monitor_has_no_start_step(self):
        wiz = Wizard(kind="monitor", step="target")
        self.assertEqual(wiz.next_step(), "mode")

    def test_describe_stored_start(self):
        from bot.menu import MenuController
        self.assertIn("从消息 88", MenuController._describe_stored_start({"start_msg_id": 88}))
        self.assertIn("2024-05-01",
                      MenuController._describe_stored_start({"start_date": "2024-05-01T00:00"}))
        self.assertIn("从头开始", MenuController._describe_stored_start({}))


# ---------------------------------------------------------------------------
# 数据库：迁移 / 任务 / 进度
# ---------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    source_chat_id INTEGER NOT NULL,
    source_topic_id INTEGER,
    target_chat_id INTEGER NOT NULL,
    target_topic_id INTEGER,
    mode TEXT DEFAULT 'copy',
    status TEXT DEFAULT 'running',
    last_synced_msg_id INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE message_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    source_msg_id INTEGER NOT NULL,
    target_msg_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class TestDatabaseMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "old.db")

    async def asyncTearDown(self):
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_migration_adds_columns_and_keeps_rows(self):
        # 用旧结构建库并写入一行
        conn = await aiosqlite.connect(self.path)
        await conn.executescript(OLD_SCHEMA)
        await conn.execute(
            "INSERT INTO tasks (type, source_chat_id, target_chat_id, last_synced_msg_id)"
            " VALUES ('sync', -1001, -1002, 777)")
        await conn.commit()
        await conn.close()

        db = Database(self.path)
        await db.init()

        cursor = await db.db.execute("PRAGMA table_info(tasks)")
        cols = {row["name"] for row in await cursor.fetchall()}
        for name in ("start_msg_id", "start_date", "total_count", "done_count",
                     "fail_count", "skip_count", "notify_chat_id", "notify_msg_id",
                     "finished_at"):
            self.assertIn(name, cols, f"缺少迁移列 {name}")

        row = await models.get_task(db, 1)
        self.assertEqual(row["last_synced_msg_id"], 777)   # 老数据保留
        self.assertEqual(row["done_count"], 0)
        await db.close()

    async def test_migration_is_idempotent(self):
        db = Database(self.path)
        await db.init()
        await db.close()
        db2 = Database(self.path)
        await db2.init()          # 二次 init 不应报错
        await db2.close()


class TestTaskProgress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = await new_db(self.tmp)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_create_task_stores_start_point(self):
        task_id = await models.create_task(
            self.db, "sync", -1001, -1002, "copy",
            start_msg_id=500, source_title="源", target_title="目标",
            notify_chat_id=-1002, created_by=42)
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["start_msg_id"], 500)
        self.assertEqual(t["notify_chat_id"], -1002)
        self.assertEqual(t["created_by"], 42)
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["done_count"], 0)

    async def test_progress_accumulates(self):
        task_id = await models.create_task(self.db, "sync", -1, -2)
        await models.update_progress(self.db, task_id, total=500)
        await models.update_progress(self.db, task_id, last_msg_id=10, done_delta=3)
        await models.update_progress(self.db, task_id, last_msg_id=20, done_delta=2,
                                     fail_delta=1, skip_delta=4)
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["total_count"], 500)
        self.assertEqual(t["done_count"], 5)
        self.assertEqual(t["fail_count"], 1)
        self.assertEqual(t["skip_count"], 4)
        self.assertEqual(t["last_synced_msg_id"], 20)

    async def test_task_notify_and_completion(self):
        task_id = await models.create_task(self.db, "sync", -1, -2)
        await models.update_task_notify(self.db, task_id, -2, 999)
        await models.update_task_status(self.db, task_id, "completed")
        t = await models.get_task(self.db, task_id)
        self.assertEqual(t["notify_msg_id"], 999)
        self.assertEqual(t["status"], "completed")
        self.assertIsNotNone(t["finished_at"])

    async def test_find_resumable_clone(self):
        task_id = await models.create_task(
            self.db, "sync", -1001, -1002, "copy",
            source_topic_id=None, target_topic_id=None)
        # 还没进度 → 不可续传
        self.assertIsNone(await models.find_resumable_clone(
            self.db, -1001, -1002, None, None))

        await models.update_progress(self.db, task_id, last_msg_id=321)
        await models.update_task_status(self.db, task_id, "paused")
        found = await models.find_resumable_clone(self.db, -1001, -1002, None, None)
        self.assertIsNotNone(found)
        self.assertEqual(found["last_synced_msg_id"], 321)

        # 不同目标不应命中
        self.assertIsNone(await models.find_resumable_clone(
            self.db, -1001, -9999, None, None))

    async def test_interrupted_clones(self):
        t1 = await models.create_task(self.db, "sync", -1, -2)
        t2 = await models.create_task(self.db, "monitor", -1, -2)
        ids = [t["id"] for t in await models.get_interrupted_clones(self.db)]
        self.assertIn(t1, ids)
        self.assertNotIn(t2, ids)   # monitor 不走克隆续传


# ---------------------------------------------------------------------------
# 起点解析（build_plan）
# ---------------------------------------------------------------------------

class TestBuildPlan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = await new_db(self.tmp)
        self.syncer = make_syncer(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_from_head(self):
        client = FakeClient(remaining=120)
        task = {"last_synced_msg_id": 0, "start_msg_id": 0, "start_date": None,
                "done_count": 0}
        plan = await self.syncer.build_plan(task, client, -1001, None)
        self.assertEqual(plan.offset_id, 0)
        self.assertFalse(plan.resumed)
        self.assertEqual(plan.total, 120)
        self.assertIn("从头开始", plan.start_desc)
        self.assertEqual(client.calls[0].get("min_id"), 0)

    async def test_from_msg_id_is_inclusive(self):
        client = FakeClient(remaining=80)
        task = {"last_synced_msg_id": 0, "start_msg_id": 123, "done_count": 0}
        plan = await self.syncer.build_plan(task, client, -1001, None)
        # reverse 模式取 id > offset，因此回退一条实现「含起始条」
        self.assertEqual(plan.offset_id, 122)
        self.assertEqual(plan.total, 80)
        self.assertIn("123", plan.start_desc)

    async def test_from_date_uses_boundary_id(self):
        client = FakeClient(remaining=50, boundary_id=777)
        task = {"last_synced_msg_id": 0, "start_msg_id": 0,
                "start_date": "2024-01-31T00:00:00+08:00", "done_count": 0}
        plan = await self.syncer.build_plan(task, client, -1001, None)
        self.assertEqual(plan.offset_id, 777)
        self.assertTrue(any("offset_date" in c for c in client.calls))

    async def test_resume_overrides_start_options(self):
        client = FakeClient(remaining=30)
        task = {"last_synced_msg_id": 900, "start_msg_id": 5,
                "start_date": "2024-01-01T00:00:00", "done_count": 70}
        plan = await self.syncer.build_plan(task, client, -1001, None)
        self.assertEqual(plan.offset_id, 900)
        self.assertTrue(plan.resumed)
        self.assertEqual(plan.total, 100)      # 30 剩余 + 已做 70
        self.assertIn("断点续传", plan.start_desc)

    async def test_count_failure_falls_back_to_stored_total(self):
        class BrokenClient:
            async def get_messages(self, *a, **kw):
                raise RuntimeError("boom")

        task = {"last_synced_msg_id": 0, "start_msg_id": 0, "total_count": 888}
        plan = await self.syncer.build_plan(task, BrokenClient(), -1001, None)
        self.assertEqual(plan.total, 888)


# ---------------------------------------------------------------------------
# 流式分批
# ---------------------------------------------------------------------------

class TestIterBatches(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = await new_db(self.tmp)
        self.syncer = make_syncer(self.db)      # batch_size=5

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def _collect(self, msgs):
        counter = {"last_id": 0, "skip": 0}
        batches = []
        async for batch in self.syncer._iter_batches(
                1, FakeIterClient(msgs), -1001, None, 0, counter):
            batches.append([m.id for m in batch])
        return batches, counter

    async def test_batches_are_chunked(self):
        msgs = [msg(i) for i in range(1, 13)]
        batches, _ = await self._collect(msgs)
        self.assertGreaterEqual(len(batches), 2)
        flat = [i for b in batches for i in b]
        self.assertEqual(flat, list(range(1, 13)))
        for b in batches[:2]:
            self.assertLessEqual(len(b), 5)

    async def test_album_never_split_across_batches(self):
        # 4 条普通 + 4 张同相册 + 4 条普通，batch=5
        msgs = ([msg(i) for i in range(1, 5)]
                + [msg(i, grouped_id=555) for i in range(5, 9)]
                + [msg(i) for i in range(9, 13)])
        batches, _ = await self._collect(msgs)
        album_batches = [b for b in batches if set(range(5, 9)) & set(b)]
        self.assertEqual(len(album_batches), 1, f"相册被切开了: {batches}")
        # 4 张图必须整组出现在同一批（可以和相邻普通消息同批）
        self.assertTrue(set(range(5, 9)) <= set(album_batches[0]),
                        f"相册成员被拆散: {batches}")

    async def test_empty_messages_are_skipped(self):
        msgs = [msg(1), msg(2, text=None, media=None), msg(3)]
        batches, counter = await self._collect(msgs)
        flat = [i for b in batches for i in b]
        self.assertEqual(flat, [1, 3])
        self.assertEqual(counter["skip"], 1)
        self.assertEqual(counter["last_id"], 2)

    async def test_cancel_stops_iteration(self):
        self.syncer._cancel_flags[1] = True
        batches, _ = await self._collect([msg(i) for i in range(1, 50)])
        self.assertEqual(batches, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
