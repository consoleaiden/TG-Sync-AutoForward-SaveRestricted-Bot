"""克隆并发闸门：CloneGate 语义 + /bf 命令（查看/设置/持久化）。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.clone_gate import CloneGate
from core.config_store import async_load_overlay
from db.database import Database


class CloneGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_access(self):
        gate = CloneGate(1)
        running = 0
        peak = 0

        async def worker():
            nonlocal running, peak
            async with gate:
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*(worker() for _ in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual(gate.active, 0)

    async def test_set_limit_higher_wakes_waiters(self):
        gate = CloneGate(1)
        inside = asyncio.Event()

        async def holder():
            async with gate:
                inside.set()
                await asyncio.sleep(0.05)

        results = []

        async def waiter(i):
            async with gate:
                results.append(i)

        h = asyncio.create_task(holder())
        await inside.wait()
        ws = [asyncio.create_task(waiter(i)) for i in range(3)]
        await asyncio.sleep(0.01)
        self.assertEqual(results, [], "上限 1 时不应有 waiter 进入")
        await gate.set_limit(3)  # 调大 → 排队者应被唤醒
        await asyncio.gather(h, *ws)
        self.assertEqual(sorted(results), [0, 1, 2])

    async def test_set_limit_lower_blocks_new(self):
        gate = CloneGate(2)
        inside = asyncio.Event()
        release = asyncio.Event()

        async def holder(i):
            async with gate:
                if i == 0:
                    inside.set()
                await release.wait()

        tasks = [asyncio.create_task(holder(i)) for i in range(2)]
        await inside.wait()
        await gate.set_limit(1)
        entered = []

        async def late():
            async with gate:
                entered.append(True)

        t = asyncio.create_task(late())
        await asyncio.sleep(0.01)
        self.assertEqual(entered, [], "active(2) >= limit(1)，新任务应排队")
        release.set()
        await asyncio.gather(*tasks, t)
        self.assertTrue(entered, "释放后应能进入")


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


class BfCommandTest(unittest.IsolatedAsyncioTestCase):
    def _find_bf_handler(self, bot):
        from telethon import events
        for ev, fn in bot.handlers:
            if not isinstance(ev, events.NewMessage):
                continue
            src = getattr(getattr(getattr(ev, "pattern", None),
                                  "__self__", None), "pattern", "")
            if src and "/bf" in src:
                return fn
        return None

    def _setup(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        with open("config.yaml", "w", encoding="utf-8") as f:
            f.write("clone:\n  max_concurrent: 1\n")
        self.db = Database(os.path.join(self.tmp, "bf.db"))
        self.bot = FakeBot()
        self.config = {"admin_ids": [7], "clone": {"max_concurrent": 1}}
        from core.monitor import MonitorManager
        self.monitor = MonitorManager(self.bot, self.bot, self.db, self.config)

    async def _teardown(self):
        await self.db.close()
        os.chdir(self.old_cwd)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_bf_show_and_set(self):
        self._setup()
        try:
            await self.db.init()
            from bot.handlers import register_handlers
            register_handlers(self.bot, self.bot, self.db, self.config,
                              self.monitor)
            fn = self._find_bf_handler(self.bot)
            self.assertIsNotNone(fn, "未注册 /bf 处理器")

            ev = FakeEvent("/bf")
            await fn(ev)
            self.assertIn("同时克隆上限: 1", ev.replies[0])

            ev2 = FakeEvent("/bf 3")
            await fn(ev2)
            self.assertIn("克隆并发上限 1 → 3", ev2.replies[0])
            # 持久化到数据库 + 内存 config 同步
            self.assertEqual(await async_load_overlay(self.db),
                             {"clone.max_concurrent": 3})
            self.assertEqual(self.config["clone"]["max_concurrent"], 3)

            ev3 = FakeEvent("/bf")
            await fn(ev3)
            self.assertIn("同时克隆上限: 3", ev3.replies[0])

            # 非法输入
            for bad in ("/bf 0", "/bf 11", "/bf abc"):
                evb = FakeEvent(bad)
                await fn(evb)
                self.assertNotIn("✅", evb.replies[0])
        finally:
            await self._teardown()

    async def test_bf_rejects_non_admin(self):
        self._setup()
        try:
            await self.db.init()
            from bot.handlers import register_handlers
            register_handlers(self.bot, self.bot, self.db, self.config,
                              self.monitor)
            fn = self._find_bf_handler(self.bot)
            ev = FakeEvent("/bf 2", sender_id=999)
            await fn(ev)
            self.assertEqual(ev.replies, ["⛔ 无权限"])
            self.assertEqual(await async_load_overlay(self.db), {})
        finally:
            await self._teardown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
