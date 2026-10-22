"""代理选优 / 保活补丁的离线单元测试（不联网、不需要凭据）。

运行::

    python -m unittest test.test_proxy_selection -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import proxy_manager as pm          # noqa: E402
from core.keepalive import install            # noqa: E402
from telethon.client.telegramclient import TelegramClient  # noqa: E402


def _ranked_item(server, port, latency, dc_ms=None, verified=True, alive=True):
    """构造一条与 mtproxy_probe.rank_results 输出结构一致的候选。"""
    item = {
        "server": server, "port": port, "latency_ms": latency,
        "verified": verified, "alive": alive,
        "secret": "ee" + "ab" * 16 + b"cloudflare.com".hex(),
    }
    if dc_ms is not None:
        item["dc_ms"] = dc_ms
        item["dc_ok"] = True
    return item


class TestPickBest(unittest.TestCase):
    def test_prefers_dc_verified_over_faster_handshake(self):
        """DC 往返通过的代理优先，即使握手更慢。"""
        ranked = [
            _ranked_item("1.1.1.1", 443, 100),            # 握手最快但没通过 DC 校验
            _ranked_item("2.2.2.2", 443, 800, dc_ms=2600),  # 真实可用
        ]
        best = pm.pick_best(ranked)
        self.assertEqual(best["server"], "2.2.2.2")
        self.assertTrue(best["dc_ok"])
        self.assertEqual(best["dc_ms"], 2600)

    def test_dc_verified_sorted_by_roundtrip(self):
        ranked = [
            _ranked_item("1.1.1.1", 443, 100, dc_ms=3000),
            _ranked_item("2.2.2.2", 443, 900, dc_ms=2500),
        ]
        self.assertEqual(pm.pick_best(ranked)["server"], "1.1.1.1")

    def test_falls_back_to_handshake_verified(self):
        ranked = [_ranked_item("1.1.1.1", 443, 100),
                  _ranked_item("2.2.2.2", 443, 200, verified=False)]
        best = pm.pick_best(ranked)
        self.assertEqual(best["server"], "1.1.1.1")
        self.assertFalse(best["dc_ok"])

    def test_all_dead_returns_none(self):
        ranked = [_ranked_item("1.1.1.1", 443, None, alive=False),
                  _ranked_item("2.2.2.2", 443, None, alive=False)]
        self.assertIsNone(pm.pick_best(ranked))


class TestValidateCandidates(unittest.IsolatedAsyncioTestCase):
    async def test_orders_by_dc_result_and_keeps_failures_as_fallback(self):
        ranked = [_ranked_item("1.1.1.1", 443, 100),
                  _ranked_item("2.2.2.2", 443, 200),
                  _ranked_item("3.3.3.3", 443, 300)]

        async def fake_roundtrip(candidate, *, timeout=0):
            # 2.2.2.2 可用；1.1.1.1 握手最快但传不了数据
            return 2100.0 if candidate["server"] == "2.2.2.2" else None

        with mock.patch.object(pm, "_dc_roundtrip", side_effect=fake_roundtrip):
            result = await pm.validate_candidates(ranked, top=3)

        self.assertEqual(result[0]["server"], "2.2.2.2")
        self.assertTrue(result[0]["dc_ok"])
        # 失败候选仍然保留，作为后续回退
        self.assertEqual({r["server"] for r in result}, {"1.1.1.1", "2.2.2.2", "3.3.3.3"})
        self.assertFalse(result[1]["dc_ok"])
        self.assertIsNone(result[1]["dc_ms"])

    async def test_no_pass_keeps_original_order(self):
        ranked = [_ranked_item("1.1.1.1", 443, 100)]

        with mock.patch.object(pm, "_dc_roundtrip", return_value=None):
            result = await pm.validate_candidates(ranked, top=3)

        self.assertEqual([r["server"] for r in result], ["1.1.1.1"])
        self.assertFalse(result[0]["dc_ok"])


class TestStateFile(unittest.TestCase):
    def test_save_state_persists_alive_and_dc_fields(self):
        """alive 必须落盘，否则 main.py 复测时会误判当前代理已失效。"""
        ranked = [_ranked_item("1.1.1.1", 443, 100, dc_ms=2500)]
        best = pm.pick_best(ranked)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy.json")
            pm.save_state(best, ranked, "https://example.invalid/top40.json", path)
            with open(path, encoding="utf-8") as f:
                state = json.load(f)

        first = state["candidates"][0]
        self.assertTrue(first["alive"])
        self.assertTrue(first["dc_ok"])
        self.assertEqual(first["dc_ms"], 2500)
        self.assertTrue(state["current"]["dc_ok"])

    def test_latency_metric_prefers_dc(self):
        from main import _latency_metric
        self.assertEqual(_latency_metric({"dc_ms": 2500, "latency_ms": 90}), 2500.0)
        self.assertEqual(_latency_metric({"latency_ms": 90}), 90.0)
        self.assertEqual(_latency_metric({}), 10 ** 9)


class TestKeepalive(unittest.TestCase):
    def test_install_patches_client_loop(self):
        original = TelegramClient._keepalive_loop
        try:
            interval = install(20)
            self.assertEqual(interval, 20.0)
            self.assertIsNot(TelegramClient._keepalive_loop, original)
        finally:
            TelegramClient._keepalive_loop = original

    def test_install_disabled_returns_zero(self):
        self.assertEqual(install(0), 0.0)

    def test_interval_has_floor(self):
        """过短的间隔没有意义，应被抬到下限 5 秒。"""
        original = TelegramClient._keepalive_loop
        try:
            self.assertEqual(install(1), 5.0)
        finally:
            TelegramClient._keepalive_loop = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
