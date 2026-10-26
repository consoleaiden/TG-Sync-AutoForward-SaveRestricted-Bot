"""启动连接自愈：代理状态过期重选 + 首次连接失败重试（不崩溃循环）。"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app_main
from core.proxy_manager import normalize_config

CAND_A = {"server": "a.example.com", "port": 443, "secret": "dd" * 16}
CAND_B = {"server": "b.example.com", "port": 443, "secret": "dd" * 16}


def _state_of(candidate: dict, updated_at: str) -> dict:
    return {"updated_at": updated_at, "source": "test",
            "current": candidate, "candidates": [candidate]}


class StateAgeTest(unittest.TestCase):
    def test_fresh_and_stale(self):
        now = datetime.now(timezone.utc)
        fresh = _state_of(CAND_A, (now - timedelta(minutes=5)).isoformat())
        stale = _state_of(CAND_A, (now - timedelta(hours=15)).isoformat())
        self.assertLess(app_main._state_age_minutes(fresh), 30)
        self.assertGreater(app_main._state_age_minutes(stale), 30)

    def test_bad_timestamp_is_infinitely_old(self):
        self.assertEqual(app_main._state_age_minutes(
            _state_of(CAND_A, "not-a-date")), float("inf"))
        self.assertEqual(app_main._state_age_minutes({}), float("inf"))


class ResolveProxyStaleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "proxy.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_stale_state_triggers_refresh(self):
        now = datetime.now(timezone.utc)
        with open(self.state_file, "w", encoding="utf-8") as f:
            import json
            json.dump(_state_of(CAND_A, (now - timedelta(hours=15)).isoformat()), f)

        config = {"proxy": {"mode": "auto", "state_file": self.state_file}}
        self.assertEqual(normalize_config(config["proxy"])["stale_minutes"], 30)

        calls = []

        async def fake_refresh(cfg):
            calls.append(cfg)
            return _state_of(CAND_B, datetime.now(timezone.utc).isoformat())

        with mock.patch.object(app_main, "run_refresh", side_effect=fake_refresh):
            result = await app_main._resolve_proxy(config)
        self.assertEqual(len(calls), 1, "过期状态应触发一轮重新选优")
        self.assertEqual(result[1][0], CAND_B["server"])

    async def test_fresh_state_is_reused(self):
        now = datetime.now(timezone.utc)
        with open(self.state_file, "w", encoding="utf-8") as f:
            import json
            json.dump(_state_of(CAND_A, (now - timedelta(minutes=2)).isoformat()), f)

        config = {"proxy": {"mode": "auto", "state_file": self.state_file}}

        async def fail_refresh(cfg):  # pragma: no cover - 不应被调用
            raise AssertionError("新鲜状态不应触发重新选优")

        with mock.patch.object(app_main, "run_refresh", side_effect=fail_refresh):
            result = await app_main._resolve_proxy(config)
        self.assertEqual(result[1][0], CAND_A["server"])


class FakeUserBotManager:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.started = 0

    async def start(self):
        self.started += 1
        if self.started <= self.fail_times:
            raise ConnectionError("Connection to Telegram failed 10 time(s)")


class FakeBot:
    async def start(self, bot_token=None):
        pass


class FakeDB:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class BootStartClientsTest(unittest.IsolatedAsyncioTestCase):
    def _config(self, mode="auto"):
        return {"bot_token": "t:1", "proxy": {"mode": mode}}

    async def test_retries_then_succeeds_in_process(self):
        config = self._config()
        ub = FakeUserBotManager(fail_times=2)
        db = FakeDB()

        async def same_refresh(cfg):
            return _state_of(CAND_A, datetime.now(timezone.utc).isoformat())

        with mock.patch.object(app_main, "run_refresh", side_effect=same_refresh), \
                mock.patch.object(app_main.asyncio, "sleep", fake_sleep):
            await app_main._boot_start_clients(
                FakeBot(), ub, db, config, proxy_candidate=CAND_A)
        self.assertEqual(ub.started, 3, "前 2 次失败后原地重试，第 3 次成功")
        self.assertFalse(db.closed)

    async def test_exits_to_apply_new_proxy(self):
        config = self._config()
        ub = FakeUserBotManager(fail_times=1)
        db = FakeDB()

        async def new_refresh(cfg):
            return _state_of(CAND_B, datetime.now(timezone.utc).isoformat())

        exited = []

        def fake_exit(code):
            exited.append(code)
            raise SystemExit(code)

        with mock.patch.object(app_main, "run_refresh", side_effect=new_refresh), \
                mock.patch.object(app_main.os, "_exit", side_effect=fake_exit):
            with self.assertRaises(SystemExit):
                await app_main._boot_start_clients(
                    FakeBot(), ub, db, config, proxy_candidate=CAND_A)
        self.assertEqual(exited, [0], "选出新代理应重启进程应用")
        self.assertTrue(db.closed)

    async def test_gives_up_after_max_attempts(self):
        config = self._config(mode="off")  # off 模式无代理可选
        ub = FakeUserBotManager(fail_times=99)
        db = FakeDB()

        with mock.patch.object(app_main.asyncio, "sleep", fake_sleep):
            with self.assertRaises(SystemExit) as ctx:
                await app_main._boot_start_clients(
                    FakeBot(), ub, db, config, proxy_candidate=None, max_attempts=3)
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(db.closed)
        self.assertEqual(ub.started, 3)


async def fake_sleep(_sec):
    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
