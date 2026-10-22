#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MTProxy 存活 / 访问速率探测引擎（纯标准库，无第三方依赖）。

实现 Telegram MTProxy 的 FakeTLS（secret 以 0xEE 开头）客户端握手：

  1. 构造一个完整的 TLS 1.3 ClientHello，SNI 取自 secret 尾部的伪装域名；
  2. random 字段（流内偏移 11..43）填入
       HMAC-SHA256(secret, 把 random 抹零后的整条 ClientHello)
     并把 digest 末 4 字节与当前 unix 时间戳（小端）异或，供服务端校验时效；
  3. 读取服务端回应：ServerHello(0x16) + ChangeCipherSpec(0x14) + ApplicationData(0x17)；
  4. 按 mtg / mtprotoproxy 的算法校验 ServerHello 摘要：
       HMAC-SHA256(secret, client_random + 服务端整包（digest 抹零）)
     校验通过 = 服务端确实认识这个 secret，而不是回落到了伪装网站。

一次探测同时产出三段耗时，用于「访问速率」排序：
  connect_ms  TCP 建连耗时（网络单程 RTT 的近似）
  tls_ms      发出 ClientHello 到收齐服务端握手包的耗时
  latency_ms  connect_ms + tls_ms，作为排序主指标

非 FakeTLS 的 secret（dd / 裸 16 字节）无法在不引入加密依赖的前提下完成
MTProto 往返校验，这里只做 TCP 建连测速，标记 mode="tcp"。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import secrets as py_secrets
import socket
import statistics
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, quote, urlparse

# --------------------------------------------------------------------------- #
# 协议常量
# --------------------------------------------------------------------------- #

DIGEST_POS = 11          # ClientHello 流内 random 字段偏移：5 record + 4 handshake + 2 version
DIGEST_LEN = 32
TLS_VERS = b"\x03\x03"

REC_HANDSHAKE = 0x16
REC_CHANGE_CIPHER = 0x14
REC_APPDATA = 0x17
HS_CLIENT_HELLO = 0x01
HS_SERVER_HELLO = 0x02

MAX_SERVER_PACKET = 16384 * 2   # 服务端握手包上限，防御异常服务端
SECRET_MIN_LEN = 17             # 1 字节前缀 + 16 字节密钥

DEFAULT_TIMEOUT = 6.0
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_CONCURRENCY = 128
DEFAULT_ATTEMPTS = 2


# --------------------------------------------------------------------------- #
# secret / 链接解析
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Secret:
    """解析后的 secret。"""

    kind: str          # faketls | obfuscated | plain | invalid
    raw: bytes
    key: bytes = b""
    domain: str = ""
    error: str = ""


def _maybe_hex(text: str) -> bytes | None:
    if len(text) % 2 or not text:
        return None
    if any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _maybe_base64(text: str) -> bytes | None:
    cleaned = text.replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=True)
    except Exception:
        return None


def parse_secret(text: str) -> Secret:
    """把 t.me 链接里的 secret 参数解析成 (类型, 密钥, 伪装域名)。"""
    text = (text or "").strip()
    if not text:
        return Secret("invalid", b"", error="secret 为空")

    raw = _maybe_hex(text)
    if raw is None:
        raw = _maybe_base64(text)
    if raw is None or not raw:
        return Secret("invalid", b"", error="secret 既不是合法 hex 也不是合法 base64")

    prefix = raw[0]
    if prefix == 0xEE:
        if len(raw) < SECRET_MIN_LEN:
            return Secret("invalid", raw, error="FakeTLS secret 长度不足 17 字节")
        domain = raw[17:].decode("ascii", errors="replace").strip()
        if not domain or any(c in domain for c in "/ ?#&="):
            return Secret("invalid", raw, error="FakeTLS secret 缺少合法伪装域名")
        return Secret("faketls", raw, key=raw[1:17], domain=domain)
    if prefix == 0xDD:
        return Secret("obfuscated", raw, key=raw[1:17] if len(raw) >= 17 else b"")
    if len(raw) == 16:
        return Secret("plain", raw, key=raw)
    return Secret("invalid", raw, error="未知 secret 前缀 0x%02x" % prefix)


@dataclass(frozen=True)
class Target:
    """一个待探测的代理。"""

    server: str
    port: int
    secret: str
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.server.lower(), self.port, self.secret)

    @property
    def link(self) -> str:
        return "https://t.me/proxy?server=%s&port=%d&secret=%s" % (
            quote(self.server, safe=""),
            self.port,
            quote(self.secret, safe=""),
        )


def parse_link(line: str) -> Target | None:
    """解析一行 t.me / tg:// 代理链接。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("tg://"):
        query = line.split("?", 1)[1] if "?" in line else ""
    elif "://" in line or line.startswith("t.me/"):
        url = line if "://" in line else "https://" + line
        parsed = urlparse(url)
        if not parsed.path.rstrip("/").endswith("proxy"):
            return None
        query = parsed.query
    else:
        return None

    params = parse_qs(query)
    server = (params.get("server") or [""])[0].strip()
    port_text = (params.get("port") or [""])[0].strip()
    secret = (params.get("secret") or [""])[0].strip()
    if not server or not port_text.isdigit() or not secret:
        return None
    port = int(port_text)
    if not (0 < port < 65536):
        return None
    return Target(server=server, port=port, secret=secret)


# --------------------------------------------------------------------------- #
# ClientHello 构造
# --------------------------------------------------------------------------- #

def _sni_extension(domain: str) -> bytes:
    name = domain.encode("ascii", errors="replace")
    entry = b"\x00" + struct.pack(">H", len(name)) + name
    listing = struct.pack(">H", len(entry)) + entry
    return b"\x00\x00" + struct.pack(">H", len(listing)) + listing


def build_client_hello(domain: str, session_id: bytes | None = None) -> bytearray:
    """构造一条浏览器风格的 TLS 1.3 ClientHello，random 字段先填零。

    返回 bytearray：调用方算出 digest 后直接回写 DIGEST_POS 处 32 字节，
    这样「抹零版」和「真实版」结构完全一致，不会出现长度/padding 漂移。
    """
    if session_id is None:
        session_id = py_secrets.token_bytes(32)
    if len(session_id) != 32:
        raise ValueError("session_id 必须是 32 字节")

    cipher_suites = bytes.fromhex(
        "130113021303c02bc02fc02cc030cca9cca8c013c014009c009d002f0035"
    )

    # x25519 公钥：内容不重要（服务端只回显自己的），但结构要合法
    key_share = b"\x00\x1d" + struct.pack(">H", 32) + py_secrets.token_bytes(32)

    extensions = b"".join([
        _sni_extension(domain),                                        # SNI
        b"\x00\x17\x00\x00",                                           # extended_master_secret
        b"\xff\x01\x00\x01\x00",                                       # renegotiation_info
        b"\x00\x0a\x00\x08\x00\x06\x00\x1d\x00\x17\x00\x18",           # supported_groups
        b"\x00\x0b\x00\x02\x01\x00",                                   # ec_point_formats
        b"\x00\x23\x00\x00",                                           # session_ticket
        b"\x00\x10\x00\x0e\x00\x0c\x02h2\x08http/1.1",                 # ALPN
        b"\x00\x05\x00\x05\x01\x00\x00\x00\x00",                       # status_request
        b"\x00\x0d\x00\x14\x00\x12\x04\x03\x08\x04\x04\x01\x05\x03"
        b"\x08\x05\x05\x01\x08\x06\x06\x01\x02\x01",                   # signature_algorithms
        b"\x00\x12\x00\x00",                                           # signed_certificate_timestamp
        b"\x00\x33" + struct.pack(">H", len(key_share) + 2)
        + struct.pack(">H", len(key_share)) + key_share,               # key_share
        b"\x00\x2d\x00\x02\x01\x01",                                   # psk_key_exchange_modes
        b"\x00\x2b\x00\x05\x04\x03\x04\x03\x03",                       # supported_versions
        b"\x00\x1b\x00\x03\x02\x00\x02",                               # compress_certificate
    ])

    # padding 到常见浏览器 ClientHello 尺寸（517 字节），让指纹更自然
    fixed = 5 + 4 + 2 + DIGEST_LEN + 1 + len(session_id) + 2 + len(cipher_suites) + 2 + 2
    pad_len = max(0, 517 - fixed - len(extensions) - 4)
    extensions += b"\x00\x15" + struct.pack(">H", pad_len) + b"\x00" * pad_len

    body = (
        TLS_VERS
        + b"\x00" * DIGEST_LEN
        + bytes([len(session_id)]) + session_id
        + struct.pack(">H", len(cipher_suites)) + cipher_suites
        + b"\x01\x00"
        + struct.pack(">H", len(extensions)) + extensions
    )
    handshake = bytes([HS_CLIENT_HELLO]) + struct.pack(">I", len(body))[1:] + body

    # record version 必须是 0x0301：mtprotoproxy 严格匹配 b"\x16\x03\x01"
    return bytearray(b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake)


def apply_client_digest(hello: bytearray, key: bytes, timestamp: int | None = None) -> bytes:
    """把 HMAC 派生的 random 写回 ClientHello，返回最终要发送的字节。"""
    if timestamp is None:
        timestamp = int(time.time())
    zeroed = bytes(hello)
    digest = hmac.new(key, zeroed, hashlib.sha256).digest()
    mask = b"\x00" * (DIGEST_LEN - 4) + struct.pack("<I", timestamp & 0xFFFFFFFF)
    random_field = bytes(a ^ b for a, b in zip(digest, mask))
    hello[DIGEST_POS:DIGEST_POS + DIGEST_LEN] = random_field
    return bytes(hello)


def client_random(packet: bytes) -> bytes:
    return packet[DIGEST_POS:DIGEST_POS + DIGEST_LEN]


def verify_server_digest(secret_key: bytes, sent_random: bytes, packet: bytes) -> bool:
    """校验服务端握手包摘要（mtg / mtprotoproxy 语义：整包参与 HMAC）。"""
    if len(packet) < DIGEST_POS + DIGEST_LEN:
        return False
    received = packet[DIGEST_POS:DIGEST_POS + DIGEST_LEN]
    zeroed = packet[:DIGEST_POS] + b"\x00" * DIGEST_LEN + packet[DIGEST_POS + DIGEST_LEN:]
    expected = hmac.new(secret_key, sent_random + zeroed, hashlib.sha256).digest()
    return hmac.compare_digest(expected, received)


def parse_records(buffer: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """切分 TLS 记录，返回 (记录列表, 已消费字节数)。不完整记录留在缓冲区。"""
    records: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 5 <= len(buffer):
        rtype = buffer[offset]
        length = struct.unpack(">H", buffer[offset + 3:offset + 5])[0]
        if length > MAX_SERVER_PACKET:
            return records, len(buffer)          # 畸形记录，停止解析
        if offset + 5 + length > len(buffer):
            break
        records.append((rtype, buffer[offset:offset + 5 + length]))
        offset += 5 + length
    return records, offset


# --------------------------------------------------------------------------- #
# 单次探测
# --------------------------------------------------------------------------- #

@dataclass
class ProbeAttempt:
    ok: bool = False
    verified: bool = False
    mode: str = "faketls"
    connect_ms: float | None = None
    tls_ms: float | None = None
    latency_ms: float | None = None
    error: str = ""

    @property
    def key(self) -> str:
        return "ok" if self.ok else self.error


async def _read_server_packet(
    reader: asyncio.StreamReader, deadline: float, grace: float = 1.0
) -> tuple[bytes, bool]:
    """收齐 ServerHello + CCS + 一条 ApplicationData，返回 (整包, 是否完整)。

    服务端通常一次 write 就把三段全发出来；如果只来了 ServerHello+CCS，
    再多等 grace 秒就收手，避免为一台半残代理耗满整个超时。
    """
    buffer = b""
    seen_hello = seen_ccs = seen_appdata = False
    hello_at: float | None = None

    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        wait = deadline - now
        if hello_at is not None:
            wait = min(wait, max(0.05, grace - (now - hello_at)))
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=wait)
        except (asyncio.TimeoutError, TimeoutError):
            break
        except (ConnectionError, OSError):
            break
        if not chunk:
            break

        buffer += chunk
        if len(buffer) > MAX_SERVER_PACKET:
            break

        records, _ = parse_records(buffer)
        for rtype, rec in records:
            payload = rec[5:]
            if rtype == REC_HANDSHAKE and payload[:1] == bytes([HS_SERVER_HELLO]):
                if hello_at is None:
                    hello_at = now
                seen_hello = True
            elif rtype == REC_CHANGE_CIPHER:
                seen_ccs = True
            elif rtype == REC_APPDATA:
                seen_appdata = True

        if seen_hello and seen_ccs and seen_appdata:
            return buffer, True

    return buffer, seen_hello and seen_ccs and seen_appdata


async def probe_once(
    target: Target,
    secret: Secret,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_TIMEOUT,
) -> ProbeAttempt:
    """对一个代理做一次完整探测。"""
    started = time.monotonic()
    attempt = ProbeAttempt(mode="faketls" if secret.kind == "faketls" else "tcp")

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.server, target.port),
            timeout=connect_timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        attempt.error = "connect_timeout"
        return attempt
    except socket.gaierror:
        attempt.error = "dns_fail"
        return attempt
    except ConnectionRefusedError:
        attempt.error = "refused"
        return attempt
    except OSError as exc:
        attempt.error = "connect_error:%s" % (exc.errno or "io")
        return attempt

    attempt.connect_ms = round((time.monotonic() - started) * 1000, 1)

    try:
        if secret.kind != "faketls":
            # 只测 TCP 可达性，不做协议校验
            attempt.ok = True
            attempt.latency_ms = attempt.connect_ms
            return attempt

        hello = build_client_hello(secret.domain)
        packet = apply_client_digest(hello, secret.key)

        send_at = time.monotonic()
        writer.write(packet)
        await writer.drain()

        raw, complete = await _read_server_packet(reader, send_at + handshake_timeout)
        attempt.tls_ms = round((time.monotonic() - send_at) * 1000, 1)

        if not raw:
            attempt.error = "no_response"
            return attempt

        records, _ = parse_records(raw)
        hello_record = next(
            (rec for rtype, rec in records
             if rtype == REC_HANDSHAKE and rec[5:6] == bytes([HS_SERVER_HELLO])),
            None,
        )
        if hello_record is None:
            if records and records[0][0] == REC_APPDATA:
                attempt.error = "not_faketls"
            else:
                attempt.error = "bad_handshake"
            return attempt

        # 摘要必须覆盖服务端整包（mtg 把 ServerHello+CCS+AppData 一次写出）
        attempt.ok = True
        attempt.verified = complete and verify_server_digest(
            secret.key, client_random(packet), raw
        )
        attempt.latency_ms = round(attempt.connect_ms + attempt.tls_ms, 1)
        return attempt
    except (ConnectionResetError, BrokenPipeError):
        attempt.error = "reset"
        return attempt
    except OSError as exc:
        attempt.error = "io_error:%s" % (exc.errno or "io")
        return attempt
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def probe_target(
    target: Target,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """多次探测一个目标，取成功样本的中位数作为速率。"""
    secret = parse_secret(target.secret)
    if secret.kind == "invalid":
        return {
            "server": target.server, "port": target.port, "secret": target.secret,
            "sources": list(target.sources), "alive": False, "verified": False,
            "mode": "invalid", "latency_ms": None, "connect_ms": None, "tls_ms": None,
            "success_rate": 0.0, "samples": 0, "error": secret.error,
        }

    results: list[ProbeAttempt] = []
    for idx in range(max(1, attempts)):
        results.append(await probe_once(
            target, secret,
            connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout,
        ))
        if idx + 1 < attempts:
            await asyncio.sleep(0.05)

    good = [r for r in results if r.ok and r.latency_ms is not None]
    errors = [r.error for r in results if not r.ok]
    error = "" if good else (errors[-1] if errors else "unknown")

    def median_of(values: list[float | None]) -> float | None:
        samples = [v for v in values if v is not None]
        return round(statistics.median(samples), 1) if samples else None

    return {
        "server": target.server,
        "port": target.port,
        "secret": target.secret,
        "sources": list(target.sources),
        "alive": bool(good),
        "verified": any(r.verified for r in good),
        "mode": secret.kind,
        "latency_ms": median_of([r.latency_ms for r in good]),
        "connect_ms": median_of([r.connect_ms for r in good]),
        "tls_ms": median_of([r.tls_ms for r in good]),
        "success_rate": round(len(good) / len(results), 3),
        "samples": len(results),
        "error": error,
    }


# --------------------------------------------------------------------------- #
# 批量探测
# --------------------------------------------------------------------------- #

async def probe_many(
    targets: Sequence[Target],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    attempts: int = DEFAULT_ATTEMPTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_TIMEOUT,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """并发探测全部目标，返回明细结果（未排序）。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(targets)
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    started = time.monotonic()

    async def worker(target: Target) -> None:
        nonlocal done
        async with semaphore:
            try:
                result = await probe_target(
                    target, attempts=attempts,
                    connect_timeout=connect_timeout, handshake_timeout=handshake_timeout,
                )
            except Exception as exc:  # 单台异常不能拖垮整批
                result = {
                    "server": target.server, "port": target.port, "secret": target.secret,
                    "sources": list(target.sources), "alive": False, "verified": False,
                    "mode": "error", "latency_ms": None, "connect_ms": None, "tls_ms": None,
                    "success_rate": 0.0, "samples": 0,
                    "error": "probe_exception:%s" % type(exc).__name__,
                }
        async with lock:
            results.append(result)
            done += 1
            if progress and (done % 100 == 0 or done == total):
                elapsed = time.monotonic() - started
                alive = sum(1 for r in results if r["alive"])
                print(
                    "  [probe] %d/%d 完成 · 存活 %d · 已用 %.1fs"
                    % (done, total, alive, elapsed),
                    file=sys.stderr, flush=True,
                )

    await asyncio.gather(*(worker(t) for t in targets))
    return results


def rank_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按可用性 + 访问速率排序。

    摘要校验通过的排最前（说明服务端确实认这个密钥），其次是能握手但校验
    不过的（多半密钥已失效，Telegram 里连不上），失效的垫底。
    """
    def tier(item: dict[str, Any]) -> int:
        if not item.get("alive") or item.get("latency_ms") is None:
            return 2
        return 0 if item.get("verified") else 1

    def sort_key(item: dict[str, Any]):
        level = tier(item)
        return (
            level,
            item.get("latency_ms") if level < 2 else 10 ** 9,
            -float(item.get("success_rate") or 0.0),
            item.get("server") or "",
        )

    ranked = sorted(results, key=sort_key)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
        item["link"] = Target(
            item["server"], item["port"], item["secret"],
            tuple(item.get("sources") or ()),
        ).link
    return ranked


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_targets(path: str) -> list[Target]:
    """从 .txt（每行一条链接）或 .json（[[{...}]] 或 [{...}]）读取目标。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    if path.endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("proxies") or data.get("targets") or []
        targets = []
        for row in data:
            if isinstance(row, str):
                parsed = parse_link(row)
            else:
                parsed = Target(
                    server=str(row["server"]),
                    port=int(row["port"]),
                    secret=str(row["secret"]),
                    sources=tuple(row.get("sources") or ()),
                )
            if parsed:
                targets.append(parsed)
        return targets

    targets = []
    for line in text.splitlines():
        parsed = parse_link(line)
        if parsed:
            targets.append(parsed)
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MTProxy 存活 / 访问速率探测")
    parser.add_argument("input", help="代理列表（.txt 每行一条链接，或 .json）")
    parser.add_argument("-o", "--output", help="结果 JSON 输出路径（默认打印到 stdout）")
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("-a", "--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--limit", type=int, default=0, help="只探测前 N 条（调试用）")
    parser.add_argument("--top", type=int, default=0, help="输出前 N 条（调试用）")
    args = parser.parse_args(argv)

    targets = load_targets(args.input)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有读到任何代理链接", file=sys.stderr)
        return 2

    print("探测 %d 个代理（并发 %d，每台 %d 次）…" % (
        len(targets), args.concurrency, args.attempts), file=sys.stderr)

    results = asyncio.run(probe_many(
        targets,
        concurrency=args.concurrency,
        attempts=args.attempts,
        connect_timeout=args.connect_timeout,
        handshake_timeout=args.timeout,
    ))
    ranked = rank_results(results)
    if args.top:
        ranked = ranked[: args.top]

    payload = {
        "tested": len(results),
        "alive": sum(1 for r in results if r["alive"]),
        "verified": sum(1 for r in results if r["verified"]),
        "proxies": ranked,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        print("已写出 %s（存活 %d / 校验通过 %d / 共 %d）" % (
            args.output, payload["alive"], payload["verified"], payload["tested"]),
            file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
