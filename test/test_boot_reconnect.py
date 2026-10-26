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
        self.proxy = None
        self.clients = []

    async def start(self):
        self.started += 1
        if self.started <= self.fail_times:
            raise ConnectionError("Connection to Telegram failed 10 time(s)")


class FakeBot:
    def __init__(self):
        self._connection = None
        self._proxy = None
        self._init_request = type("_IR", (), {"proxy": None})()

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

    async def test_new_proxy_applied_in_place(self):
        """选出新代理：就地应用到客户端并重试连接，不再退出进程。"""
        config = self._config()
        ub = FakeUserBotManager(fail_times=1)
        db = FakeDB()
        bot = FakeBot()

        async def new_refresh(cfg):
            return _state_of(CAND_B, datetime.now(timezone.utc).isoformat())

        with mock.patch.object(app_main, "run_refresh", side_effect=new_refresh), \
                mock.patch.object(app_main.asyncio, "sleep", fake_sleep):
            await app_main._boot_start_clients(
                bot, ub, db, config, proxy_candidate=CAND_A)
        self.assertEqual(ub.started, 2, "第 1 次失败换新代理，第 2 次成功")
        self.assertFalse(db.closed)
        # Bot 已挂上 CAND_B 的连接类与代理元组；manager 的 proxy 同步更新
        from core.proxy_manager import build_telethon_proxy
        conn_cls, proxy_tuple = build_telethon_proxy(CAND_B)
        self.assertIs(bot._connection, conn_cls)
        self.assertEqual(bot._proxy, proxy_tuple)
        self.assertEqual(ub.proxy, (conn_cls, proxy_tuple))

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


class ReconnectProxyCheckTest(unittest.IsolatedAsyncioTestCase):
    """断线代理自愈：第 1 次失败即重选、新代理就地返回、无新代理兜底退出。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "proxy.json")
        with open(self.state_file, "w", encoding="utf-8") as f:
            import json
            json.dump(_state_of(CAND_A, datetime.now(timezone.utc).isoformat()), f)
        self.config = {"proxy": {"mode": "auto", "state_file": self.state_file}}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _run(self, streak, refresh_result, mode="auto"):
        calls = []

        async def fake_refresh(cfg):
            calls.append(cfg)
            if isinstance(refresh_result, Exception):
                raise refresh_result
            return refresh_result

        with mock.patch.object(app_main, "run_refresh", side_effect=fake_refresh):
            result = await app_main._reconnect_proxy_check(
                streak, {"proxy": {"mode": mode, "state_file": self.state_file}},
                current=CAND_A)
        return result, calls

    async def test_first_failure_returns_new_candidate(self):
        state = _state_of(CAND_B, datetime.now(timezone.utc).isoformat())
        result, calls = await self._run(1, state)
        self.assertEqual(len(calls), 1, "第 1 次重连失败就应触发重选")
        self.assertEqual(result, CAND_B, "新代理应就地返回（不再要求重启）")

    async def test_same_candidate_returns_none(self):
        state = _state_of(CAND_A, datetime.now(timezone.utc).isoformat())
        result, _ = await self._run(1, state)
        self.assertIsNone(result, "重选结果与当前相同则不切换")

    async def test_non_trigger_streak_skips_refresh(self):
        result, calls = await self._run(2, None)
        self.assertIsNone(result)
        self.assertEqual(calls, [], "第 2 次失败不重选（1、3、6、9…才触发）")

    async def test_mode_off_never_refreshes(self):
        result, calls = await self._run(1, None, mode="off")
        self.assertIsNone(result)
        self.assertEqual(calls, [], "off 模式（直连）不做代理重选")

    async def test_gives_up_at_streak_nine_when_refresh_fails(self):
        with self.assertRaises(SystemExit) as ctx:
            await self._run(9, RuntimeError("all mirrors dead"))
        self.assertEqual(ctx.exception.code, 1)

    async def test_gives_up_at_streak_nine_when_no_new_proxy(self):
        state = _state_of(CAND_A, datetime.now(timezone.utc).isoformat())
        with self.assertRaises(SystemExit) as ctx:
            await self._run(9, state)
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
