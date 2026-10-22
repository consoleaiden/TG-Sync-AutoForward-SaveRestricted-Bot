#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telethon MTProto 代理 FakeTLS 连接支持。

背景：Telethon 内置的 ConnectionTcpMTProxy* 系列只支持裸 16 字节 / 0xDD 型
secret，没有实现 FakeTLS（0xEE 型 secret）的 TLS 伪装握手，因此无法连接
绝大多数公共 MTProto 代理（telegram-mtproto-proxy-list 榜单中的代理几乎
全部为 FakeTLS 类型）。

本模块实现 ConnectionTcpMTProxyFakeTLS，协议行为与 mtg / mtprotoproxy 及
Telegram 官方客户端一致：

  1. TCP 连上代理后，发送真实 TLS 1.3 ClientHello（SNI 取 secret 尾部的
     伪装域名，random 字段 = HMAC-SHA256(secret_key, ClientHello[:random])
     末 4 字节异或 unix 时间戳）；
  2. 收齐服务端 ServerHello + ChangeCipherSpec + ApplicationData，并按
     mtg 语义校验服务端整包 HMAC 摘要（区分「密钥有效」与「假握手回落」）；
  3. 握手完成后，把底层流替换为 TLS 记录封装层：
       - 写方向：应用数据按 ≤16384 字节切块，每块封装为
         ``17 03 03 <len:2B> payload`` 的 ApplicationData 记录；
       - 读方向：解析 TLS 记录头，剥离封装、跳过 ChangeCipherSpec /
         close_notify，把 payload 还原为连续字节流；
     FakeTLS 本身不做真实 TLS 加密，只是伪装；
  4. 在封装层之上照常运行 Telethon 的 MTProxy 混淆（MTProxyIO）与
     padded intermediate 传输，后续 MTProto 流程与原生连接完全一致。

关于传输类型（重要）：
  MTProxy 混淆头最后 4 字节（帧内偏移 56..60）是「连接类型」标记，服务端
  据此决定传输协议。mtg（当前绝大多数公共代理的实现）**只接受
  ``dd dd dd dd``（padded intermediate）**，传 abridged（efefefef）或
  intermediate（eeeeeeee）会被判定为非法握手并回落到伪装网站
  （返回 HTTP 400 Bad Request）。Telegram 官方客户端在 FakeTLS 下同样使用
  padded intermediate，因此这里固定 packet_codec = RandomizedIntermediatePacketCodec。

proxy 参数格式与内置 MTProxy 连接类一致::

    proxy = (host, port, secret_hex)
    # secret_hex 与 t.me/proxy 链接中 secret 参数相同，例如
    # "ee" + 32 位 hex 密钥 + 伪装域名的 hex
    TelegramClient(..., connection=ConnectionTcpMTProxyFakeTLS, proxy=proxy)

握手 / 记录封装的协议细节参考：
  * upstream: consoleaiden/telegram-mtproto-proxy-list (scripts/mtproxy_probe.py)
  * alexbers/mtprotoproxy (FakeTLSStreamReader / FakeTLSStreamWriter)
  * 9seconds/mtg (协议与摘要算法)
"""

from __future__ import annotations

import asyncio
import logging
import time

from telethon.network.connection.tcpintermediate import (
    RandomizedIntermediatePacketCodec,
)
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
    MTProxyIO,
)

from .mtproxy_probe import (
    _read_server_packet,
    apply_client_digest,
    build_client_hello,
    client_random,
    parse_secret,
    verify_server_digest,
)

logger = logging.getLogger(__name__)

# TLS 记录类型
REC_CHANGE_CIPHER = 0x14
REC_APPDATA = 0x17
REC_CLOSE_NOTIFY_ALERT = 0x15  # warning: close_notify

# 单条 TLS 记录 payload 上限（TLS 规范 2^14）
MAX_RECORD_PAYLOAD = 16384


class _FakeTLSRecordWriter:
    """把上层字节流封装为 TLS ApplicationData 记录后写入底层 writer。

    需要实现 Telethon Connection 对 writer 的全部用法：
    write / drain / close / wait_closed / is_closing。
    """

    def __init__(self, upstream):
        self._upstream = upstream

    def write(self, data: bytes) -> None:
        for start in range(0, len(data), MAX_RECORD_PAYLOAD):
            chunk = data[start:start + MAX_RECORD_PAYLOAD]
            self._upstream.write(
                b"\x17\x03\x03"
                + len(chunk).to_bytes(2, "big")
                + chunk
            )

    async def drain(self) -> None:
        await self._upstream.drain()

    def close(self) -> None:
        self._upstream.close()

    async def wait_closed(self) -> None:
        try:
            await self._upstream.wait_closed()
        except Exception:
            pass

    def is_closing(self) -> bool:
        return self._upstream.is_closing()


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """通过 FakeTLS（0xEE 型 secret）MTProto 代理连接 Telegram。

    传输固定为 padded intermediate（混淆头 tag = dd dd dd dd），与 mtg 及
    官方客户端一致。
    """

    packet_codec = RandomizedIntermediatePacketCodec

    # 建连后判断「代理是否拒绝当前传输」的等待时长（秒）
    eof_probe_timeout = 1.0

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        # 保存原始 secret（ee + 16 字节密钥 + 伪装域名 hex）供握手使用；
        # 父类会把 normalize 后的 16 字节密钥存到 self._secret 供混淆层使用
        secret_raw = proxy[2] if proxy else ""
        self._fake_tls_secret_hex = (
            secret_raw if isinstance(secret_raw, str) else bytes(secret_raw).hex()
        )
        self._fake_tls_pump_task: asyncio.Task | None = None
        super().__init__(ip, port, dc_id, loggers=loggers,
                         proxy=proxy, local_addr=local_addr)

    # ------------------------------------------------------------------ #
    # 连接主流程
    # ------------------------------------------------------------------ #
    async def _connect(self, timeout=None, ssl=None):
        # TcpMTProxy.__init__ 已把 self._ip / self._port 换成代理地址，
        # 且未向 Connection 传 proxy，因此这里是对代理的直连
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host=self._ip, port=self._port),
            timeout=timeout,
        )

        try:
            await self._fake_tls_handshake(timeout)
        except Exception:
            try:
                self._writer.close()
            except Exception:
                pass
            raise

        # 用 TLS 记录封装层替换底层流，之后 Telethon 标准流程照常执行
        plain_reader = asyncio.StreamReader()
        self._fake_tls_pump_task = asyncio.get_running_loop().create_task(
            self._tls_record_pump(plain_reader, self._reader)
        )
        self._reader = plain_reader
        self._writer = _FakeTLSRecordWriter(self._writer)

        self._codec = self.packet_codec(self)
        self._init_conn()          # MTProxyIO：64 字节混淆头经 TLS 记录封装写出
        await self._writer.drain()

        # 与内置 TcpMTProxy._connect 相同的「代理拒绝 codec」探测：
        # 若代理在收到混淆头后立即关闭连接，视为代理不可用。
        # 已有数据（或已 EOF）时无需等待；超时缩短到 1s，避免 FastTelethon
        # 多连接时每连接累加 2s 开销。
        if not self._reader.at_eof() and not self._reader._buffer:
            try:
                await asyncio.wait_for(self._reader._wait_for_data("proxy"),
                                       self.eof_probe_timeout)
            except asyncio.TimeoutError:
                pass
            except Exception:
                await asyncio.sleep(self.eof_probe_timeout)

        if self._reader.at_eof():
            await self.disconnect()
            raise ConnectionError(
                "Proxy closed the connection after sending initial payload")

    async def disconnect(self):
        try:
            await super().disconnect()
        finally:
            task, self._fake_tls_pump_task = self._fake_tls_pump_task, None
            if task and not task.done():
                task.cancel()

    # ------------------------------------------------------------------ #
    # FakeTLS 握手
    # ------------------------------------------------------------------ #
    async def _fake_tls_handshake(self, timeout) -> None:
        secret = parse_secret(self._fake_tls_secret_hex)
        if secret.kind != "faketls":
            raise ConnectionError(
                "FakeTLS 连接类要求 0xEE 型 secret，当前类型: %s" % secret.kind)

        hello = build_client_hello(secret.domain)
        packet = apply_client_digest(hello, secret.key)

        self._writer.write(packet)
        await self._writer.drain()

        deadline = time.monotonic() + (timeout or 15.0)
        raw, complete = await _read_server_packet(self._reader, deadline)

        if not raw:
            raise ConnectionError("FakeTLS 握手失败: 服务端无响应")
        if not complete or not verify_server_digest(
                secret.key, client_random(packet), raw):
            raise ConnectionError(
                "FakeTLS 握手失败: 服务端摘要校验未通过（secret 可能已失效）")
        logger.debug("FakeTLS 握手成功 (server=%s:%s)", self._ip, self._port)

    # ------------------------------------------------------------------ #
    # TLS 记录读取泵：把底层 TLS 记录流还原为连续字节流
    # ------------------------------------------------------------------ #
    async def _tls_record_pump(self, plain: asyncio.StreamReader,
                               raw: asyncio.StreamReader) -> None:
        """循环读取底层 TLS 记录，把 ApplicationData payload 喂给 plain。

        伪装证书 / HTTP 头等握手期数据已在握手阶段消费完毕，这里只处理
        握手后的记录；close_notify 或异常视为流结束。
        """
        buf = b""
        try:
            while True:
                # 读记录头 5 字节：type(1) + version(2) + length(2)
                while len(buf) < 5:
                    chunk = await raw.read(MAX_RECORD_PAYLOAD)
                    if not chunk:
                        plain.feed_eof()
                        return
                    buf += chunk

                rec_type = buf[0]
                length = int.from_bytes(buf[3:5], "big")
                total = 5 + length

                while len(buf) < total:
                    chunk = await raw.read(max(MAX_RECORD_PAYLOAD,
                                               total - len(buf)))
                    if not chunk:
                        plain.feed_eof()
                        return
                    buf += chunk

                payload = buf[5:total]
                buf = buf[total:]

                if rec_type == REC_APPDATA and payload:
                    plain.feed_data(payload)
                elif rec_type == REC_CLOSE_NOTIFY_ALERT:
                    plain.feed_eof()
                    return
                # 其他记录类型（CCS 等）忽略
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                plain.feed_eof()
            except Exception:
                pass
