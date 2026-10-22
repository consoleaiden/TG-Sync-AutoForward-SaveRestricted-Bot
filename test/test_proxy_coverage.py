"""代理覆盖度守卫：确保没有新增代码绕过代理直连 Telegram。

两条约束：
1. 静态约束 —— 除 ``core/proxy_manager.py`` 的 ``create_client`` 外，任何模块都不得
   直接 ``TelegramClient(...)``；新增客户端必须经统一入口，代理参数才不会漏传。
2. 动态约束 —— ``create_client`` 必须把 ``connection``/``proxy`` 真正注入到客户端，
   并能在缺失代理时明确报错。

运行::

    python -m unittest test.test_proxy_coverage -v
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telethon.sessions import MemorySession  # noqa: E402

from core.proxy_manager import (  # noqa: E402
    build_client_kwargs,
    create_client,
    verify_proxy_applied,
)

# 唯一允许直接构造 TelegramClient 的文件（统一注入代理的收口点）
CHOKEPOINT = ROOT / "core" / "proxy_manager.py"
SKIP_DIRS = {".git", "__pycache__", "test", "reports", ".venv", "venv"}


def _iter_sources():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


class TestStaticCoverage(unittest.TestCase):
    def test_no_direct_client_construction_outside_chokepoint(self):
        offenders = []
        for path in _iter_sources():
            if path.resolve() == CHOKEPOINT.resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == "TelegramClient":
                    offenders.append("%s:%d" % (path.relative_to(ROOT), node.lineno))

        self.assertEqual(
            offenders, [],
            "以下位置直接构造了 TelegramClient，会绕过代理统一注入，"
            "请改用 core.proxy_manager.create_client()：\n  " + "\n  ".join(offenders))

    def test_all_scripts_import_chokepoint(self):
        """会联网的脚本必须从 proxy_manager 取客户端工厂。"""
        for name in ("scripts/login_userbot.py", "main.py", "core/userbot_manager.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("create_client", text, "%s 未使用统一客户端入口" % name)


class TestCreateClient(unittest.TestCase):
    def _proxy(self):
        from core.faketls_mtproxy import ConnectionTcpMTProxyFakeTLS
        secret = "ee" + "cd" * 16 + b"cloudflare.com".hex()
        return ConnectionTcpMTProxyFakeTLS, ("127.0.0.1", 443, secret)

    def test_proxy_injected(self):
        proxy = self._proxy()
        client = create_client(MemorySession(), 12345, "0" * 32, proxy=proxy)
        self.assertIs(client._connection, proxy[0])
        self.assertEqual(client._proxy, proxy[1])
        self.assertTrue(verify_proxy_applied(client, "unit-test"))

    def test_direct_mode_keeps_no_proxy(self):
        client = create_client(MemorySession(), 12345, "0" * 32, proxy=None)
        self.assertIsNone(client._proxy)
        self.assertFalse(verify_proxy_applied(client, "unit-test-direct"))

    def test_build_client_kwargs(self):
        proxy = self._proxy()
        kwargs = build_client_kwargs(proxy)
        self.assertEqual(set(kwargs), {"connection", "proxy"})
        self.assertEqual(build_client_kwargs(None), {})

    def test_extra_kwargs_preserved(self):
        client = create_client(MemorySession(), 12345, "0" * 32,
                               proxy=self._proxy(), connection_retries=1,
                               retry_delay=1, timeout=7)
        self.assertEqual(client._timeout, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
