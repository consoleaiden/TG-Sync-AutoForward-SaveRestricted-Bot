"""缩短 Telethon 心跳周期，避免经 MTProto 代理时空载连接被掐断。

背景（实测结论，勿轻易改回）：
    Telethon 的 ``TelegramClient._keepalive_loop`` 里心跳间隔是**硬编码 60 秒**
    （见 ``telethon/client/updates.py``）。而公共 MTProto 代理（mtg 系）普遍在
    45~55 秒无数据时关闭连接。两者错位的结果是：只要 Bot/UserBot 空闲一会儿，
    连接必然被代理关闭，日志出现

        Server closed the connection: 0 bytes read on a total of 4 expected bytes

    随后自动重连。单次重连约 2 秒，通常不影响功能，但在大文件转发、等待验证码、
    长轮询场景下会打断进行中的请求，且日志噪音极大。

解决方案：
    用同样的逻辑替换 ``_keepalive_loop``，仅把等待时长改为可配置（默认 20 秒），
    确保心跳始终早于代理的空闲超时发出。心跳使用 Telethon 自身的
    ``_sender._keepalive_ping``，因此不会破坏 pong 追踪与重连判定。
"""

from __future__ import annotations

import asyncio
import logging
import random

from telethon import utils
from telethon.client.telegramclient import TelegramClient

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 20.0
_INSTALLED = False
_INTERVAL = DEFAULT_INTERVAL


def _patch(interval: float) -> None:
    """把 TelegramClient 的心跳循环替换为可调间隔版本。"""

    async def _keepalive_loop(self) -> None:
        rnd = lambda: random.randrange(-2**63, 2**63)  # noqa: E731
        while self.is_connected():
            try:
                await asyncio.wait_for(self.disconnected, timeout=interval)
                continue  # 连接断开，交给外层重连逻辑
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 与上游实现保持一致，忽略瞬时异常
                continue

            await self._clean_exported_senders()

            # 传输层未就绪时不要堆积 ping，否则重连后会被批量发出
            if not self._sender._transport_connected():
                continue

            try:
                self._sender._keepalive_ping(rnd())
            except (ConnectionError, asyncio.CancelledError):
                return

            await self._save_states_and_entities()
            await utils.maybe_async(self.session.save())

    TelegramClient._keepalive_loop = _keepalive_loop


def install(interval: float | int | None = None) -> float:
    """安装心跳补丁，返回实际生效的间隔（秒）。

    幂等：重复调用只会更新间隔，不会重复替换方法。``interval`` 为 0 或负数时
    表示不启用（保持 Telethon 原生 60 秒）。
    """
    global _INSTALLED, _INTERVAL

    if interval is None:
        interval = DEFAULT_INTERVAL
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL

    if interval <= 0:
        logger.info("心跳补丁未启用（interval<=0），沿用 Telethon 默认 60 秒")
        return 0.0

    # 间隔过长等于没解决代理空闲超时问题，这里做一个下限保护
    interval = max(5.0, interval)

    if not _INSTALLED:
        _patch(interval)
        _INSTALLED = True
        logger.info("已启用连接保活补丁：每 %.0f 秒发送一次心跳（Telethon 默认 60 秒）", interval)
    elif interval != _INTERVAL:
        _patch(interval)
        logger.info("连接保活间隔已更新为 %.0f 秒", interval)

    _INTERVAL = interval
    return interval
