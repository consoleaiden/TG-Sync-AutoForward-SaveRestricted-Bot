"""数据落盘相关测试：路径解析、持久化自检、WAL、在线备份与菜单入口。

不连 Telegram、不碰真实数据库：全部在临时目录里跑。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.menu import MenuController
from core.storage import (
    DEFAULT_DB_PATH,
    audit_db_path,
    backup_settings,
    format_audit,
    persistence_warning,
    resolve_db_path,
    wal_enabled,
)
from db import models
from db.backup import backup_database, list_backups, restore_hint
from db.database import Database


class TestDbPathResolution(unittest.TestCase):
    def test_default_path(self):
        self.assertEqual(resolve_db_path({}, {}), DEFAULT_DB_PATH)
        self.assertEqual(resolve_db_path(None, {}), DEFAULT_DB_PATH)

    def test_config_overrides_default(self):
        cfg = {"storage": {"db_path": "custom/tasks.db"}}
        self.assertEqual(resolve_db_path(cfg, {}), "custom/tasks.db")

    def test_env_overrides_config(self):
        cfg = {"storage": {"db_path": "custom/tasks.db"}}
        env = {"TG_DB_PATH": "/app/db/bot.db"}
        self.assertEqual(resolve_db_path(cfg, env), "/app/db/bot.db")

    def test_blank_values_fall_back(self):
        env = {"TG_DB_PATH": "   "}
        cfg = {"storage": {"db_path": ""}}
        self.assertEqual(resolve_db_path(cfg, env), DEFAULT_DB_PATH)

    def test_wal_and_backup_settings(self):
        self.assertTrue(wal_enabled({}))
        self.assertFalse(wal_enabled({"storage": {"wal": False}}))
        self.assertFalse(wal_enabled({"storage": {"wal": "off"}}))

        self.assertEqual(backup_settings({}), {"backup_dir": None, "keep": 7})
        opts = backup_settings({"storage": {"backup_dir": "data/bk", "backup_keep": "3"}})
        self.assertEqual(opts, {"backup_dir": "data/bk", "keep": 3})
        # 非法值回退到默认，且不会小于 1
        self.assertEqual(backup_settings({"storage": {"backup_keep": "abc"}})["keep"], 7)
        self.assertEqual(backup_settings({"storage": {"backup_keep": 0}})["keep"], 1)


class TestPersistenceAudit(unittest.TestCase):
    def test_persisted_inside_mounted_dir(self):
        audit = audit_db_path("/app/data/bot.db",
                              env={"TG_PERSIST_DIRS": "/app/data"},
                              in_container=True, config={})
        self.assertTrue(audit["persisted"])

    def test_not_persisted_outside_mounted_dir(self):
        audit = audit_db_path("/app/tasks/bot.db",
                              env={"TG_PERSIST_DIRS": "/app/data,/app/sessions"},
                              in_container=True, config={})
        self.assertFalse(audit["persisted"])
        warning = persistence_warning(audit, "/app/tasks/bot.db")
        self.assertIsNotNone(warning)
        self.assertIn("/app/data", warning)
        self.assertIn("重启", warning + "重启")   # 文案含可执行指引

    def test_container_default_dirs(self):
        audit = audit_db_path("/app/data/bot.db", env={}, in_container=True)
        self.assertTrue(audit["persisted"])
        audit = audit_db_path("/opt/bot.db", env={}, in_container=True)
        self.assertFalse(audit["persisted"])

    def test_host_mode_needs_no_judgement(self):
        audit = audit_db_path("data/bot.db", env={}, in_container=False)
        self.assertIsNone(audit["persisted"])
        self.assertIsNone(persistence_warning(audit, "data/bot.db"))

    def test_format_audit_mentions_state(self):
        audit = audit_db_path("/app/data/bot.db",
                              env={"TG_PERSIST_DIRS": "/app/data"},
                              in_container=True)
        text = format_audit(audit)
        self.assertIn("已持久化", text)
        self.assertIn("bot.db", text)


class TestDatabaseWALAndBackup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bot.db")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_wal_enabled_and_tables_created(self):
        db = Database(self.db_path)
        await db.init()
        try:
            cur = await db.db.execute("PRAGMA journal_mode")
            mode = (await cur.fetchone())[0]
            self.assertEqual(str(mode).lower(), "wal")
            cur = await db.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in await cur.fetchall()}
            self.assertIn("tasks", tables)
            self.assertIn("message_map", tables)
        finally:
            await db.close()

    async def test_wal_can_be_disabled(self):
        db = Database(self.db_path, wal=False)
        await db.init()
        try:
            cur = await db.db.execute("PRAGMA journal_mode")
            self.assertNotEqual(str((await cur.fetchone())[0]).lower(), "wal")
        finally:
            await db.close()

    async def test_backup_contains_tasks(self):
        db = Database(self.db_path)
        await db.init()
        try:
            task_id = await models.create_task(db, "clone", -100111, -100222)
            info = await asyncio.to_thread(backup_database, self.db_path, keep=7)
        finally:
            await db.close()

        self.assertTrue(os.path.exists(info["path"]))
        self.assertGreater(info["size"], 0)
        self.assertEqual(info["total"], 1)

        import sqlite3
        conn = sqlite3.connect(info["path"])
        try:
            row = conn.execute("SELECT id, source_chat_id FROM tasks").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (task_id, -100111))

    async def test_retention_keeps_latest(self):
        db = Database(self.db_path)
        await db.init()
        await db.close()
        for i in range(1, 6):
            backup_database(self.db_path, keep=2, name=f"2026010{i}-000000")
        backups = [os.path.basename(p) for p in list_backups(
            os.path.join(self.tmp.name, "backups"))]
        self.assertEqual(len(backups), 2)
        self.assertIn("bot-20260105-000000.db", backups)

    async def test_backup_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            backup_database(os.path.join(self.tmp.name, "nope.db"))

    async def test_restore_hint_lists_wal_files(self):
        hint = restore_hint("/app/data/bot.db", "/app/data/backups/bot-1.db")
        self.assertIn("bot.db-wal", hint)
        self.assertIn("bot.db-shm", hint)


class FakeEvent:
    def __init__(self):
        self.answers = []
        self.edits = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append(text)


class TestMenuBackupEntry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bot.db")
        self.menu = MenuController(
            None,
            db=SimpleNamespace(db_path=self.db_path),
            config={},
            is_admin=lambda *_: True,
            format_display=lambda x: str(x),
            resolve_source=None, resolve_target=None,
            create_task=None, stop_task=None, resume_task=None,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_main_menu_has_backup_button(self):
        _text, buttons = self.menu._main_menu()
        labels = [b.text for row in buttons for b in row]
        self.assertTrue(any("备份" in l for l in labels), labels)

    async def test_backup_action_writes_file_and_reports(self):
        db = Database(self.db_path)
        await db.init()
        await models.create_task(db, "clone", -100111, -100222)
        await db.close()

        ev = FakeEvent()
        await self.menu._dispatch(ev, "backup")
        self.assertTrue(ev.edits, "备份后应编辑消息返回结果")
        self.assertIn("备份完成", ev.edits[-1])
        self.assertIn("bot-", ev.edits[-1])
        self.assertEqual(len(list_backups(os.path.join(self.tmp.name, "backups"))), 1)

    async def test_backup_action_handles_missing_db(self):
        ev = FakeEvent()
        await self.menu._dispatch(ev, "backup")
        self.assertFalse(ev.edits)
        self.assertTrue(ev.answers)
        self.assertIn("不存在", ev.answers[-1][0])


if __name__ == "__main__":
    unittest.main()
