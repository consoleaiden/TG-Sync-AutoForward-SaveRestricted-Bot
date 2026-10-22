"""handlers 注册装配测试：确认菜单与命令能一起注册，不出现接线错误。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.handlers import HandlerRuntime, register_handlers
from core.monitor import MonitorManager
from db.database import Database


class FakeBot:
    """只实现 on() 装饰器，记录注册到的事件处理器。"""

    def __init__(self):
        self.handlers = []

    def on(self, event):
        def deco(fn):
            self.handlers.append((event, fn))
            return fn
        return deco


class HandlersWiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "wiring.db"))
        await self.db.init()
        self.bot = FakeBot()
        self.config = {
            "admin_ids": [7],
            "allow_public_resolve": True,
            "rate_limit": {"batch_size": 100, "forward_interval": [0, 0]},
            "clone": {"auto_resume": True, "batch_size": 50},
        }

    async def asyncTearDown(self):
        await self.db.close()
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    async def test_register_handlers_returns_runtime(self):
        monitor = MonitorManager(self.bot, self.bot, self.db, self.config)
        runtime = register_handlers(self.bot, self.bot, self.db, self.config, monitor)
        self.assertIsInstance(runtime, HandlerRuntime)
        self.assertTrue(len(self.bot.handlers) > 10, "处理器注册数量异常")

    async def test_menu_callback_handler_registered(self):
        monitor = MonitorManager(self.bot, self.bot, self.db, self.config)
        register_handlers(self.bot, self.bot, self.db, self.config, monitor)

        cb_events = [e for e, _fn in self.bot.handlers
                     if type(e).__name__ == "CallbackQuery"]
        self.assertTrue(cb_events, "没有注册任何 CallbackQuery 处理器")

        menu_ev = next((e for e in cb_events
                        if getattr(e, "match", None)
                        and e.match(b"m:new:clone")), None)
        self.assertIsNotNone(menu_ev, "未注册菜单回调 ^m:")
        # 菜单回调只接 m: 前缀，不应吞掉旧的任务详情回调
        self.assertIsNone(menu_ev.match(b"task:1"))

    async def test_auto_resume_noop_when_nothing_interrupted(self):
        monitor = MonitorManager(self.bot, self.bot, self.db, self.config)
        runtime = register_handlers(self.bot, self.bot, self.db, self.config, monitor)
        self.assertEqual(await runtime.auto_resume_clones(), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
