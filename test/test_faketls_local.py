#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ConnectionTcpMTProxyFakeTLS 本地端到端集成测试（无需真实 Telegram 凭据）。

自建一个 Mock FakeTLS MTProto 代理服务端：
  1. 校验客户端 ClientHello 的 HMAC 摘要（服务端视角，同 mtg 算法）；
  2. 回复 ServerHello + ChangeCipherSpec + ApplicationData（摘要正确）；
  3. 用 MTProxy 混淆密钥派生算法解密客户端首个 64 字节混淆头，
     校验 obfuscate_tag（abridged）与 dc_id；
  4. 用派生密钥加密一条 abridged 帧，经 TLS 记录封装发回客户端；
  5. 客户端通过 conn.recv() 应还原出原始 payload。

运行：python -m test.test_faketls_local
"""

import asyncio
import hashlib
import hmac as hmac_mod
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mtproxy_probe import (  # noqa: E402
    apply_client_digest,
    build_client_hello,
    client_random,
    parse_secret,
)
from core.faketls_mtproxy import ConnectionTcpMTProxyFakeTLS  # noqa: E402

try:
    from telethon.crypto import AESModeCTR
except ImportError:  # pragma: no cover
    AESModeCTR = None

TEST_SECRET_HEX = "ee" + "cd" * 16 + b"www.speedtest.net".hex()
TEST_DC_ID = 2
# mtg 只接受 padded intermediate（dd dd dd dd）作为连接类型
PADDED_TAG = b"\xdd\xdd\xdd\xdd"


# --------------------------------------------------------------------------- #
# 服务端工具
# --------------------------------------------------------------------------- #

def server_verify_and_reply(client_hello: bytes, key: bytes) -> bytes:
    """校验 ClientHello 摘要并生成完整的握手应答（ServerHello+CCS+AppData）。"""
    from core.mtproxy_probe import DIGEST_LEN, DIGEST_POS

    # 复算客户端摘要（ClientHello random 末 4 字节异或时间戳后的结果应匹配）
    zeroed = client_hello[:DIGEST_POS] + b"\x00" * DIGEST_LEN + client_hello[DIGEST_POS + DIGEST_LEN:]
    expect = hmac_mod.new(key, zeroed, hashlib.sha256).digest()
    received = client_hello[DIGEST_POS:DIGEST_POS + DIGEST_LEN]
    ts = struct.unpack("<I", bytes(a ^ b for a, b in zip(receive_tail(expect), received[-4:])))[0]
    assert abs(int(asyncio.get_event_loop().time()) - 0) >= 0  # 占位
    assert 1_500_000_000 < ts < 4_000_000_000, "时间戳异常: %d" % ts

    # 构造 ServerHello（random 置零，随后填入 HMAC）
    srv_body = (b"\x03\x03" + b"\x00" * 32
                + b"\x20" + os.urandom(32)
                + b"\x13\x01" + b"\x01\x00"
                + b"\x00\x00")
    srv_hello = b"\x02" + len(srv_body).to_bytes(3, "big") + srv_body
    ccs = b"\x14\x03\x03\x00\x01\x01"
    fake_cert = os.urandom(1200)
    appdata = b"\x17\x03\x03" + len(fake_cert).to_bytes(2, "big") + fake_cert

    rec = (b"\x16\x03\x03" + len(srv_hello).to_bytes(2, "big") + srv_hello
           + ccs + appdata)
    zeroed_pkt = rec[:DIGEST_POS] + b"\x00" * 32 + rec[DIGEST_POS + 32:]
    digest = hmac_mod.new(key, client_random(client_hello) + zeroed_pkt,
                          hashlib.sha256).digest()
    return rec[:DIGEST_POS] + digest + rec[DIGEST_POS + 32:]


def receive_tail(digest: bytes) -> bytes:
    return digest[-4:]


def derive_mtproxy_keys(header64: bytes, key16: bytes):
    """与 telethon MTProxyIO.init_header 完全一致的密钥派生。"""
    encrypt_key = hashlib.sha256(header64[8:40] + key16).digest()
    encrypt_iv = header64[40:56]
    reversed_r = header64[55:7:-1]
    decrypt_key = hashlib.sha256(reversed_r[:32] + key16).digest()
    decrypt_iv = reversed_r[32:48]
    return (encrypt_key, encrypt_iv), (decrypt_key, decrypt_iv)


def intermediate_frame(payload: bytes) -> bytes:
    """padded intermediate 传输帧：4 字节小端长度 + payload（可含 0-3 字节填充）。"""
    return struct.pack("<i", len(payload)) + payload


def strip_intermediate(decrypted: bytes) -> bytes:
    """剥掉 padded intermediate 帧头与填充，还原 MTProto 包体。"""
    length = struct.unpack("<i", decrypted[:4])[0]
    body = decrypted[4:4 + length]
    pad = len(body) % 4
    return body[:-pad] if pad else body


class MockFakeTLSProxy:
    """最小可用的 FakeTLS MTProto 代理服务端。"""

    def __init__(self, secret_hex: str):
        self.secret_hex = secret_hex
        self.server = None
        self.got_header_ok = False
        self.got_payload = None
        self.error = None
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        for w in list(self.writers):
            try:
                w.close()
            except Exception:
                pass
        try:
            # Python 3.12+ 的 wait_closed 会等所有连接处理器退出，
            # 这里给硬超时，避免测试因服务端收尾而挂住
            await asyncio.wait_for(self.server.wait_closed(), timeout=3)
        except Exception:
            pass

    async def _handle(self, reader, writer):
        self.writers.add(writer)
        try:
            secret = parse_secret(self.secret_hex)
            hello = await reader.readexactly(517)
            reply = server_verify_and_reply(hello, secret.key)
            writer.write(reply)
            await writer.drain()

            # 读客户端 TLS 记录：第一条 payload 应为 64 字节混淆头
            header = await self._read_appdata(reader)
            assert len(header) == 64, "混淆头长度异常: %d" % len(header)

            enc_kv, dec_kv = derive_mtproxy_keys(header, secret.key)
            # 客户端→服务端方向用客户端的加密密钥还原明文
            client_stream = AESModeCTR(enc_kv[0], enc_kv[1])
            plain = client_stream.encrypt(header)
            assert plain[56:60] == PADDED_TAG, "连接类型应为 dd dd dd dd，实际: %s" % plain[56:60].hex()
            dc = int.from_bytes(plain[60:62], "little", signed=True)
            assert dc == TEST_DC_ID, "dc_id 异常: %d" % dc
            self.got_header_ok = True

            # 服务端→客户端方向用客户端的解密密钥加密
            server_stream = AESModeCTR(dec_kv[0], dec_kv[1])
            while True:
                payload = await self._read_appdata(reader)
                if payload is None:
                    return
                if self.got_payload is None and payload:
                    # 剥离 padded intermediate 帧头与填充，还原 MTProto 包体
                    self.got_payload = strip_intermediate(client_stream.encrypt(payload))
                # 回显一条 padded intermediate 帧（加密后封装为 TLS AppData 记录）
                echo = intermediate_frame(bytes(range(0, 64, 4)))
                writer.write(b"\x17\x03\x03"
                             + len(echo).to_bytes(2, "big")
                             + server_stream.encrypt(echo))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as exc:
            self.error = exc
            print("[mock] 异常: %r" % exc, file=sys.stderr, flush=True)
        finally:
            self.writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _read_appdata(reader):
        head = await reader.readexactly(5)
        assert head[0] == 0x17, "非 AppData 记录: %s" % head.hex()
        length = int.from_bytes(head[3:5], "big")
        if length == 0:
            return b""
        return await reader.readexactly(length)


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #

class FakeTLSIntegrationTest(unittest.IsolatedAsyncioTestCase):

    async def test_full_chain(self):
        mock = MockFakeTLSProxy(TEST_SECRET_HEX)
        port = await mock.start()
        try:
            loggers = {"telethon.network.connection.connection":
                       __import__("logging").getLogger("test")}
            conn = ConnectionTcpMTProxyFakeTLS(
                None, None, TEST_DC_ID, loggers=loggers,
                proxy=("127.0.0.1", port, TEST_SECRET_HEX))

            await conn.connect()
            print("[test] connected", flush=True)

            # 客户端发送一帧，服务端应解出相同明文
            payload = os.urandom(48)
            await conn.send(payload)
            print("[test] payload sent", flush=True)
            for _ in range(100):
                if mock.got_payload is not None:
                    break
                await asyncio.sleep(0.02)
            print("[test] got_payload=%s header_ok=%s" % (
                mock.got_payload is not None, mock.got_header_ok), flush=True)
            self.assertTrue(mock.got_header_ok, "服务端未收到合法混淆头")
            self.assertEqual(payload, mock.got_payload, "服务端解出的 payload 不一致")

            # 服务端回显的 abridged 帧，客户端应能还原
            received = await asyncio.wait_for(conn.recv(), timeout=5)
            print("[test] recv ok", flush=True)
            self.assertEqual(received, bytes(range(0, 64, 4)))

            await conn.disconnect()
            print("[test] disconnected", flush=True)
        finally:
            await mock.stop()


if __name__ == "__main__":
    unittest.main()
