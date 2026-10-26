"""代理热切换：不重启进程，把已连接的 Telethon 客户端切到新代理。

原理：Telethon 官方支持运行中更换代理（``client.set_proxy()`` 的文档注明
"will be used on next (re)connection"）——改完代理属性后
``disconnect() + connect()`` 即用新代理重建连接。本项目按 secret 类型
区分三种连接类（FakeTLS / Randomized / Abridged），连接类是构造时确定的
属性，热切换时需要与代理元组一并替换。

对每个客户端的流程：
  1. 记录旧 (连接类, 代理元组)；
  2. 换上新连接类与代理（先换类再设代理，保证 ``_init_request.proxy``
     按新连接类的 MTProxy 语义计算）；
  3. 已连接则 ``disconnect()`` 后限时 ``connect()``；
  4. 失败回滚到旧配置并尝试重连旧代理。

与 main.py 主循环的协作：热切换主动断开 Bot 会让
``bot.run_until_disconnected()`` 返回、进入应用层重连分支（30 分钟后）；
届时客户端早已被本模块重连成功，而 ``connect()`` 对已连接客户端是
空操作（Telethon 内部判断 sender 已连接直接返回），不会产生竞争。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("tg_forward_bot.proxy_hotswap")

CONNECT_TIMEOUT = 60.0     # 单个客户端重连限时（秒）


def apply_proxy_to_client(client, conn_cls, proxy_tuple) -> None:
    """把 (连接类, 代理元组) 写进客户端，下次 (重) 连接时生效。

    与 Telethon 自带 ``set_proxy()`` 语义一致，额外支持替换连接类
    （set_proxy 不换类）。必须先设 ``_connection`` 再算 init_proxy。
    """
    from telethon.network.connection import TcpMTProxy
    from telethon.tl.types import InputClientProxy

    client._connection = conn_cls
    init_proxy = None
    if conn_cls is not None and issubclass(conn_cls, TcpMTProxy):
        init_proxy = InputClientProxy(*conn_cls.address_info(proxy_tuple))
    client._init_request.proxy = init_proxy
    client._proxy = proxy_tuple

    # 存活连接的注入：与 set_proxy 相同——自动重连场景用的是 sender 里
    # 已构造好的 connection 对象，只有直接改它才能让自动重连也走新代理。
    sender = getattr(client, "_sender", None)
    conn = getattr(sender, "_connection", None) if sender else None
    if conn is not None:
        if isinstance(conn, TcpMTProxy):
            conn._ip = proxy_tuple[0]
            conn._port = proxy_tuple[1]
        else:
            conn._proxy = proxy_tuple


async def hot_swap_proxy(clients, candidate, *,
                         connect_timeout: float = CONNECT_TIMEOUT,
                         labels: list[str] | None = None) -> list[dict]:
    """把一批客户端热切换到 candidate 代理，逐个执行互不影响。

    返回逐客户端结果列表::

        {"label": "Bot", "ok": True}
        {"label": "UserBot#1", "ok": False, "error": "...",
         "rolled_back": True, "rollback_error": None}
    """
    from .proxy_manager import build_telethon_proxy

    conn_cls, proxy_tuple = build_telethon_proxy(candidate)

    results: list[dict] = []
    for i, client in enumerate(clients):
        label = labels[i] if labels and i < len(labels) else "client#%d" % i
        entry: dict = {"label": label, "ok": False}
        old_conn = getattr(client, "_connection", None)
        old_proxy = getattr(client, "_proxy", None)

        async def _reconnect(c=client) -> None:
            if c.is_connected():
                await c.disconnect()
            await asyncio.wait_for(c.connect(), connect_timeout)

        try:
            apply_proxy_to_client(client, conn_cls, proxy_tuple)
            await _reconnect()
            entry["ok"] = True
            logger.info("%s 已热切换到代理 %s:%s（连接类 %s）",
                        label, candidate["server"], candidate["port"],
                        conn_cls.__name__)
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            logger.warning("%s 热切换失败（%s），回滚旧代理", label, entry["error"])
            try:
                # 旧代理为 None（proxy.mode=off 直连）时同样正确恢复
                apply_proxy_to_client(client, old_conn, old_proxy)
                await _reconnect()
                entry["rolled_back"] = True
                logger.info("%s 已回滚到原代理并重连成功", label)
            except Exception as exc2:
                entry["rolled_back"] = False
                entry["rollback_error"] = str(exc2)
                logger.error("%s 回滚失败：%s（客户端可能处于断开状态，"
                             "等待应用层重连/重启）", label, exc2)
        results.append(entry)
    return results
