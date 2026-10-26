"""代理热切换（core/proxy_hotswap）：属性替换、逐客户端切换、失败回滚。

用桩客户端模拟 Telethon 的 _connection/_proxy/_init_request/
is_connected/disconnect/connect 接口，验证：
- 成功路径：连接类与代理元组被替换，先断开再重连；
- 失败路径：回滚旧连接类与旧代理并重连成功；
- 回滚也失败：记录 rollback_error，不抛出；
- 传入非法候选（build_telethon_proxy 抛 ValueError）时不碰任何客户端；
- init_request.proxy 按 MTProxy 语义计算（FakeTLS/明文均走 InputClientProxy）。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import proxy_hotswap  # noqa: E402
from core.proxy_manager import build_telethon_proxy  # noqa: E402
from telethon.network.connection import (  # noqa: E402
    ConnectionTcpAbridged,
    TcpMTProxy,
)
from telethon.tl.types import InputClientProxy  # noqa: E402


class _InitRequest:
    def __init__(self):
        self.proxy = None


class _StubSender:
    def __init__(self, conn=None):
        self._connection = conn


class _StubConn:
    """模拟一个已挂到 sender 上的非 MTProxy 连接对象。"""

    def __init__(self):
        self._proxy = "old-proxy-tuple"


class _StubMTConn(TcpMTProxy):
    """模拟 sender 上已有的 MTProxy 连接（仅为了 isinstance 分支）。"""

    def __init__(self):  # 不调用 Telethon 的 __init__，只放字段
        self._ip = "1.2.3.4"
        self._port = 443


class StubClient:
    def __init__(self, *, connected=True, fail_connect_with=None,
                 conn_cls=None, proxy=None):
        self._connection = conn_cls if conn_cls is not None else \
            ConnectionTcpAbridged
        self._proxy = proxy
        self._init_request = _InitRequest()
        self._sender = None
        self._connected = connected
        self._fail_connect_with = fail_connect_with
        self.disconnect_count = 0
        self.connect_count = 0

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self.disconnect_count += 1
        self._connected = False

    async def connect(self):
        self.connect_count += 1
        if self._fail_connect_with is not None:
            raise self._fail_connect_with
        self._connected = True
        return True


def _candidate(secret="dd" * 16):
    return {"server": "new.example", "port": 443, "secret": secret}


class ApplyProxyTest(unittest.TestCase):
    def test_mtproxy_conn_class_computes_init_proxy(self):
        c = StubClient()
        conn_cls, proxy_tuple = build_telethon_proxy(_candidate())
        proxy_hotswap.apply_proxy_to_client(c, conn_cls, proxy_tuple)
        self.assertIs(c._connection, conn_cls)
        self.assertEqual(c._proxy, proxy_tuple)
        self.assertIsInstance(c._init_request.proxy, InputClientProxy)
        self.assertEqual(c._init_request.proxy.address, "new.example")
        self.assertEqual(c._init_request.proxy.port, 443)

    def test_non_proxy_conn_class_clears_init_proxy(self):
        c = StubClient()
        proxy_hotswap.apply_proxy_to_client(c, ConnectionTcpAbridged, None)
        self.assertIsNone(c._init_request.proxy)
        self.assertIsNone(c._proxy)

    def test_live_sender_connection_injected(self):
        # sender 上是普通连接 → 注入 _proxy
        c = StubClient()
        live = _StubConn()
        c._sender = _StubSender(live)
        conn_cls, proxy_tuple = build_telethon_proxy(_candidate())
        proxy_hotswap.apply_proxy_to_client(c, conn_cls, proxy_tuple)
        self.assertEqual(live._proxy, proxy_tuple)

        # sender 上是 MTProxy 连接 → 改 _ip/_port
        c2 = StubClient()
        live2 = _StubMTConn()
        c2._sender = _StubSender(live2)
        proxy_hotswap.apply_proxy_to_client(c2, conn_cls, proxy_tuple)
        self.assertEqual(live2._ip, "new.example")
        self.assertEqual(live2._port, 443)


class HotSwapTest(unittest.TestCase):
    def test_success_swaps_and_reconnects(self):
        c = StubClient(connected=True, conn_cls=ConnectionTcpAbridged,
                       proxy="old")
        results = asyncio.run(proxy_hotswap.hot_swap_proxy(
            [c], _candidate(), labels=["Bot"]))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(c.disconnect_count, 1)
        self.assertEqual(c.connect_count, 1)
        conn_cls, proxy_tuple = build_telethon_proxy(_candidate())
        self.assertIs(c._connection, conn_cls)
        self.assertEqual(c._proxy, proxy_tuple)

    def test_success_when_originally_disconnected(self):
        c = StubClient(connected=False, proxy=None)
        results = asyncio.run(proxy_hotswap.hot_swap_proxy([c], _candidate()))
        self.assertTrue(results[0]["ok"])
        self.assertEqual(c.disconnect_count, 0)  # 本来就没连，无需断开
        self.assertEqual(c.connect_count, 1)

    def test_failure_rolls_back(self):
        # 新代理连不上 → 回滚旧代理且旧代理重连成功
        c = StubClient(connected=True, conn_cls=ConnectionTcpAbridged,
                       proxy="old")
        original_connect = StubClient.connect

        async def connect_fails_new_only(self):
            # 仅在挂新代理时失败：_proxy 是元组说明已应用新代理
            if isinstance(self._proxy, tuple):
                raise OSError("new proxy dead")
            return await original_connect(self)

        with mock.patch.object(
                StubClient, "connect", connect_fails_new_only):
            results = asyncio.run(proxy_hotswap.hot_swap_proxy(
                [c], _candidate(), labels=["Bot"]))
        self.assertFalse(results[0]["ok"])
        self.assertIn("OSError", results[0]["error"])
        self.assertTrue(results[0]["rolled_back"])
        self.assertEqual(c._connection, ConnectionTcpAbridged)
        self.assertEqual(c._proxy, "old")
        self.assertTrue(c._connected)

    def test_double_failure_records_rollback_error(self):
        c = StubClient(connected=True, fail_connect_with=OSError("all dead"))
        results = asyncio.run(proxy_hotswap.hot_swap_proxy([c], _candidate()))
        self.assertFalse(results[0]["ok"])
        self.assertFalse(results[0]["rolled_back"])
        self.assertTrue(results[0]["rollback_error"])

    def test_invalid_candidate_touches_nothing(self):
        c = StubClient(connected=True)
        bad = {"server": "x", "port": 443, "secret": "zz-invalid",
               "kind": "unknown"}
        with self.assertRaises(ValueError):
            asyncio.run(proxy_hotswap.hot_swap_proxy([c], bad))
        self.assertEqual(c.connect_count, 0)
        self.assertEqual(c.disconnect_count, 0)

    def test_multiple_clients_independent(self):
        good = StubClient(connected=True, proxy="old")
        bad = StubClient(connected=True, fail_connect_with=OSError("dead"))
        results = asyncio.run(proxy_hotswap.hot_swap_proxy(
            [good, bad], _candidate(), labels=["Bot", "UserBot#1"]))
        self.assertEqual([r["label"] for r in results], ["Bot", "UserBot#1"])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(good._connected)

    def test_labels_default(self):
        c = StubClient(connected=False)
        results = asyncio.run(proxy_hotswap.hot_swap_proxy([c], _candidate()))
        self.assertEqual(results[0]["label"], "client#0")


if __name__ == "__main__":
    unittest.main()
