"""config_store：配置覆盖层（app_settings 持久化）与 /cfg 命令。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import (
    apply_overlay, async_load_overlay, async_save_overlay, parse_value,
)
from db.database import Database


class ParseValueTest(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(parse_value("900"), 900)
        self.assertEqual(parse_value("0.5"), 0.5)
        self.assertIs(parse_value("true"), True)
        self.assertIs(parse_value("false"), False)
        self.assertEqual(parse_value("[1, 2]"), [1, 2])
        self.assertEqual(parse_value("[0.3, 0.8]"), [0.3, 0.8])

    def test_plain_string_fallback(self):
        self.assertEqual(parse_value("abc"), "abc")
        self.assertEqual(parse_value("3-6"), "3-6")


class ApplyOverlayTest(unittest.TestCase):
    def test_merges_into_config(self):
        config = {"transfer": {"strategy_timeout": 1200}, "clone": {}}
        out = apply_overlay(config, {
            "transfer.strategy_timeout": 900,
            "clone.max_concurrent": 2,
        })
        self.assertIs(out, config)
        self.assertEqual(config["transfer"]["strategy_timeout"], 900)
        self.assertEqual(config["clone"]["max_concurrent"], 2)

    def test_skips_bad_paths(self):
        config = {"transfer": {}}
        apply_overlay(config, {"badpath": 1, "transfer.ok": 2})
        self.assertEqual(config, {"transfer": {"ok": 2}})

    def test_skips_non_dict_section(self):
        config = {"bot_token": "xxx"}
        apply_overlay(config, {"bot_token.x": 1})
        self.assertEqual(config["bot_token"], "xxx")


class OverlayPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self):
        tmp = tempfile.mkdtemp()
        try:
            db = Database(os.path.join(tmp, "cfg.db"))
            await db.init()
            try:
                self.assertEqual(await async_load_overlay(db), {})
                await async_save_overlay(db, {"transfer.strategy_timeout": 900})
                self.assertEqual(await async_load_overlay(db),
                                 {"transfer.strategy_timeout": 900})
                # 保存是整体写入语义：合并由调用方负责
                await async_save_overlay(db, {"transfer.strategy_timeout": 900,
                                              "clone.max_concurrent": 2})
                self.assertEqual(await async_load_overlay(db),
                                 {"transfer.strategy_timeout": 900,
                                  "clone.max_concurrent": 2})
            finally:
                await db.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class FakeBot:
    def __init__(self):
        self.handlers = []

    def on(self, event):
        def deco(fn):
            self.handlers.append((event, fn))
            return fn
        return deco


class FakeEvent:
    def __init__(self, text, sender_id=7):
        self.raw_text = text
        self.sender_id = sender_id
        self.is_private = True
        self.replies = []

    async def reply(self, text, **_kw):
        self.replies.append(text)

    async def answer(self, *_a, **_kw):
        pass


class CfgCommandTest(unittest.IsolatedAsyncioTestCase):
    def _find_cfg_handler(self, bot):
        from telethon import events
        for ev, fn in bot.handlers:
            if not isinstance(ev, events.NewMessage):
                continue
            src = getattr(getattr(getattr(ev, "pattern", None),
                                  "__self__", None), "pattern", "")
            if src and "/cfg" in src:
                return fn
        return None

    async def test_cfg_set_list_reset(self):
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write("transfer:\n  strategy_timeout: 1200\n")
            db = Database(os.path.join(tmp, "cfg-cmd.db"))
            await db.init()
            bot = FakeBot()
            config = {"admin_ids": [7], "transfer": {"strategy_timeout": 1200}}
            from core.monitor import MonitorManager
            monitor = MonitorManager(bot, bot, db, config)
            from bot.handlers import register_handlers
            register_handlers(bot, bot, db, config, monitor)
            try:
                fn = self._find_cfg_handler(bot)
                self.assertIsNotNone(fn, "未注册 /cfg 处理器")

                ev = FakeEvent("/cfg")
                await fn(ev)
                self.assertIn("没有配置覆盖项", ev.replies[0])

                ev2 = FakeEvent("/cfg transfer.strategy_timeout 900")
                await fn(ev2)
                self.assertIn("已覆盖 transfer.strategy_timeout = 900", ev2.replies[0])
                # 内存 config 已更新（新构造组件可见）
                self.assertEqual(config["transfer"]["strategy_timeout"], 900)
                # 数据库已持久化
                self.assertEqual(await async_load_overlay(db),
                                 {"transfer.strategy_timeout": 900})

                ev3 = FakeEvent("/cfg")
                await fn(ev3)
                self.assertIn("transfer.strategy_timeout = 900", ev3.replies[0])

                ev4 = FakeEvent("/cfg transfer.strategy_timeout reset")
                await fn(ev4)
                self.assertIn("已移除", ev4.replies[0])
                self.assertEqual(await async_load_overlay(db), {})
                # 内存值回退为磁盘值
                self.assertEqual(config["transfer"]["strategy_timeout"], 1200)
            finally:
                await db.close()
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_cfg_rejects_non_admin(self):
        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            db = Database(os.path.join(tmp, "cfg2.db"))
            await db.init()
            bot = FakeBot()
            config = {"admin_ids": [7]}
            from core.monitor import MonitorManager
            monitor = MonitorManager(bot, bot, db, config)
            from bot.handlers import register_handlers
            register_handlers(bot, bot, db, config, monitor)
            try:
                fn = self._find_cfg_handler(bot)
                ev = FakeEvent("/cfg transfer.ok 1", sender_id=999)
                await fn(ev)
                self.assertEqual(ev.replies, ["⛔ 无权限"])
            finally:
                await db.close()
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
