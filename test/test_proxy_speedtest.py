"""代理测速命令（/speedall、/speedtop）后端：榜单解析、URL 推导、选优落盘、异常路径。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import proxy_manager, proxy_speedtest  # noqa: E402


def _ok(server, latency, secret="dd" * 16):
    return {
        "server": server, "port": 443, "secret": secret, "sources": [],
        "alive": True, "verified": True, "mode": "faketls",
        "latency_ms": latency, "connect_ms": latency, "tls_ms": latency,
        "success_rate": 1.0, "samples": 2,
    }


class ParsePayloadTest(unittest.TestCase):
    def test_dict_format_top40(self):
        data = {"proxies": [
            {"server": "a.com", "port": 443, "secret": "dd" * 16,
             "verified": True, "latency_ms": 100, "sources": ["x"]},
        ]}
        out = proxy_speedtest.parse_proxy_payload(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["server"], "a.com")
        self.assertEqual(out[0]["port"], 443)
        self.assertTrue(out[0]["verified"])

    def test_array_format_proxies_json(self):
        fields = ["server", "port", "secret", "sources", "alive",
                  "verified", "latency_ms", "connect_ms", "tls_ms",
                  "success_rate", "error"]
        data = {"fields": fields, "proxies": [
            ["b.com", 443, "dd" * 16, ["y"], True, True, 55.5, 40.0, 30.0, 0.9, ""],
            ["c.com", 8080, "dd" * 16, [], False, False, None, None, None, 0.0, "dead"],
        ]}
        out = proxy_speedtest.parse_proxy_payload(data)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["latency_ms"], 55.5)
        self.assertEqual(out[1]["server"], "c.com")

    def test_dedup_and_invalid_rows(self):
        data = {"proxies": [
            {"server": "a.com", "port": 443, "secret": "dd" * 16},
            {"server": "a.com", "port": 443, "secret": "dd" * 16},  # 重复
            {"server": "", "port": 443, "secret": "dd" * 16},        # 空 server
            {"server": "b.com", "port": "bad", "secret": "dd" * 16},  # 坏端口
        ]}
        out = proxy_speedtest.parse_proxy_payload(data)
        self.assertEqual([p["server"] for p in out], ["a.com"])

    def test_invalid_payloads_raise(self):
        with self.assertRaises(ValueError):
            proxy_speedtest.parse_proxy_payload({"proxies": []})
        with self.assertRaises(ValueError):
            proxy_speedtest.parse_proxy_payload({"proxies": [[1, 2]]})  # 无 fields
        with self.assertRaises(ValueError):
            proxy_speedtest.parse_proxy_payload("not-a-dict")


class DeriveUrlsTest(unittest.TestCase):
    def test_top40_uses_sources_as_is(self):
        sources = ["https://mirror.example/gh/repo@main/top40.json"]
        self.assertEqual(proxy_speedtest.derive_urls("top40", sources), sources)

    def test_all_replaces_filename(self):
        sources = ["https://mirror.example/gh/repo@main/top40.json"]
        urls = proxy_speedtest.derive_urls("all", sources)
        self.assertEqual(urls,
                         ["https://mirror.example/gh/repo@main/proxies.json"])

    def test_empty_sources_falls_back_to_repo(self):
        urls = proxy_speedtest.derive_urls("top40", [])
        self.assertEqual(len(urls), len(proxy_speedtest.DEFAULT_SOURCES))
        self.assertTrue(all(proxy_speedtest.REPO in u for u in urls))


class RunSpeedTestTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_success_sets_best_and_saves_state(self):
        tmp = tempfile.mkdtemp()
        state_file = os.path.join(tmp, "proxy.json")
        proxies = [
            {"server": "fast.com", "port": 443, "secret": "dd" * 16,
             "latency_ms": 50, "verified": True, "sources": []},
            {"server": "slow.com", "port": 443, "secret": "dd" * 16,
             "latency_ms": 300, "verified": True, "sources": []},
            {"server": "dead.com", "port": 443, "secret": "dd" * 16,
             "latency_ms": None, "verified": False, "sources": []},
        ]
        reports = []

        async def fake_fetch(kind, sources=None, timeout=None):
            return list(proxies), "https://mirror.example/top40.json"

        async def fake_probe(target, **kw):
            if target.server == "dead.com":
                raise TimeoutError("connect timeout")
            return _ok(target.server, 50 if target.server == "fast.com" else 300)

        async def fake_dc(candidate, **kw):
            return 100.0

        async def cb(done, total, alive):
            reports.append((done, total, alive))

        cfg = {"state_file": state_file, "sources": [], "mode": "auto",
               "probe": {"concurrency": 2, "attempts": 1, "timeout": 1,
                         "connect_timeout": 1, "dc_validate_top": 5,
                         "dc_validate_timeout": 1},
               "stale_minutes": 30}
        with mock.patch.object(proxy_speedtest, "fetch_proxy_list", fake_fetch), \
             mock.patch.object(proxy_speedtest, "probe_target", fake_probe), \
             mock.patch.object(proxy_manager, "_dc_roundtrip", fake_dc):
            rep = self._run(proxy_speedtest.run_speed_test(
                "top40", cfg, progress_cb=cb))

        self.assertEqual(rep["total"], 3)
        self.assertEqual(rep["alive"], 2)
        self.assertEqual(rep["failed"], 1)
        self.assertEqual(rep["best"]["server"], "fast.com")
        self.assertTrue(rep["best"]["dc_ok"])
        self.assertTrue(os.path.exists(state_file))
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["current"]["server"], "fast.com")
        # 最终有一次强制进度回调（done=total）
        self.assertEqual(reports[-1], (3, 3, 2))

    def test_all_dead_raises_and_keeps_state(self):
        tmp = tempfile.mkdtemp()
        state_file = os.path.join(tmp, "proxy.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write('{"current": {"server": "old", "port": 1, "secret": "s"}}')

        proxies = [{"server": f"d{i}.com", "port": 443, "secret": "dd" * 16,
                    "latency_ms": None, "verified": False, "sources": []}
                   for i in range(3)]

        async def fake_fetch(kind, sources=None, timeout=None):
            return list(proxies), "https://mirror.example/top40.json"

        async def fake_probe(target, **kw):
            return {"server": target.server, "port": target.port,
                    "secret": target.secret, "sources": [], "alive": False,
                    "verified": False, "mode": "error", "latency_ms": None,
                    "connect_ms": None, "tls_ms": None, "success_rate": 0.0,
                    "samples": 0, "error": "timeout"}

        cfg = {"state_file": state_file, "sources": [], "mode": "auto",
               "probe": {"concurrency": 2, "attempts": 1, "timeout": 1,
                         "connect_timeout": 1, "dc_validate_top": 5,
                         "dc_validate_timeout": 1},
               "stale_minutes": 30}
        with mock.patch.object(proxy_speedtest, "fetch_proxy_list", fake_fetch), \
             mock.patch.object(proxy_speedtest, "probe_target", fake_probe):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(proxy_speedtest.run_speed_test("all", cfg))

        self.assertIn("全部不可用", str(ctx.exception))
        with open(state_file, encoding="utf-8") as f:
            self.assertIn("old", f.read(), "全部失败时旧状态必须保持不动")


if __name__ == "__main__":
    unittest.main()


class SpeedCommandsWiringTest(unittest.IsolatedAsyncioTestCase):
    """接线：/speedall 与 /speedtop 已注册、鉴权生效、路由到正确榜单。"""

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
            kind = "speedall" if "speedall" in text else "speedtop"
            self.pattern_match = type("PM", (), {
                "group": staticmethod(lambda i=0: kind if i == 1 else text)})()

        async def reply(self, text, **_kw):
            self.replies.append(text)

        async def answer(self, *_a, **_kw):
            pass

    def _find_handler(self, bot):
        from telethon import events
        for ev, fn in bot.handlers:
            if not isinstance(ev, events.NewMessage):
                continue
            p = getattr(ev, "pattern", None)
            src = getattr(getattr(p, "__self__", None), "pattern", "")
            if "speedall" in src:
                return fn
        return None

    async def test_registered_and_admin_only(self):
        from bot.handlers import register_handlers
        from core.monitor import MonitorManager
        from db.database import Database

        tmp = tempfile.mkdtemp()
        db = Database(os.path.join(tmp, "w.db"))
        await db.init()
        try:
            bot = self.FakeBot()
            config = {"admin_ids": [7], "rate_limit": {},
                      "proxy": {"mode": "off", "sources": []}}
            monitor = MonitorManager(bot, bot, db, config)
            register_handlers(bot, bot, db, config, monitor)
            fn = self._find_handler(bot)
            self.assertIsNotNone(fn, "未注册 /speedall|/speedtop 处理器")

            # off 模式：不启动测速，直接提示
            ev = self.FakeEvent("/speedtop")
            await fn(ev)
            self.assertIn("proxy.mode=off", ev.replies[0])

            # 无权限
            ev2 = self.FakeEvent("/speedall", sender_id=999)
            await fn(ev2)
            self.assertEqual(ev2.replies, ["⛔ 无权限"])
        finally:
            await db.close()
