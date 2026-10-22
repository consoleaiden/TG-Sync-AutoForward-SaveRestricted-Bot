#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MTProto 代理自动选优管理。

流程：
  1. 从 telegram-mtproto-proxy-list 项目拉取 top40.json（多镜像依次回退，
     以适配国内网络访问 GitHub 不稳定的情况）；
  2. 在本机对榜单代理做真实 FakeTLS 握手测速（复用 core/mtproxy_probe.py
     纯标准库探测引擎），榜单耗时来自 GitHub Actions runner，与本机体验
     可能差异很大，因此本地复测是必须的；
  3. 选出「密钥校验通过且本机访问最快」的代理，原子写入状态文件
     （默认 data/proxy.json），并保留前 N 名候选供诊断；
  4. 全部失败时保持旧状态文件不动，避免用空数据覆盖可用配置。

状态文件被 main.py 在启动时读取并转成 Telethon 的
(connection 类, proxy 元组)：
  * 0xEE 型 secret → core.faketls_mtproxy.ConnectionTcpMTProxyFakeTLS
  * 0xDD 型 secret → telethon 内置 ConnectionTcpMTProxyRandomizedIntermediate
  * 裸 16 字节     → telethon 内置 ConnectionTcpMTProxyAbridged

CLI 手动操作（在项目根目录执行）::

    python -m core.proxy_manager --once        # 立即执行一次选优并写状态
    python -m core.proxy_manager --test        # 只测速打印榜单，不写状态
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .mtproxy_probe import probe_many, rank_results

logger = logging.getLogger("tg_forward_bot.proxy_manager")

# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #

REPO = "consoleaiden/telegram-mtproto-proxy-list"

# top40.json 镜像源：依次尝试，直到成功。
# fastly 放首位：实测 cdn.jsdelivr.net 在国内网络常被 reset（Errno 104）。
DEFAULT_SOURCES = [
    "https://fastly.jsdelivr.net/gh/%s@main/top40.json" % REPO,
    "https://cdn.jsdelivr.net/gh/%s@main/top40.json" % REPO,
    "https://gcore.jsdelivr.net/gh/%s@main/top40.json" % REPO,
    "https://raw.githubusercontent.com/%s/main/top40.json" % REPO,
    "https://gh-proxy.com/raw.githubusercontent.com/%s/main/top40.json" % REPO,
]

DEFAULT_STATE_FILE = "data/proxy.json"
DEFAULT_CANDIDATES = 5          # 状态文件中保留的候选数量（诊断用）

PROBE_CONCURRENCY = 40
PROBE_ATTEMPTS = 2
PROBE_TIMEOUT = 6.0
PROBE_CONNECT_TIMEOUT = 5.0
FETCH_TIMEOUT = 20.0

# 真实 DC 往返校验：握手通过 ≠ 能传数据（部分代理会对真实流量直接断连），
# 因此对握手排序前 N 名再做一次 req_pq_multi 往返，只有拿到 resPQ 才算可用。
DC_VALIDATE_TOP = 8
DC_VALIDATE_TIMEOUT = 8.0
DC_VALIDATE_CONCURRENCY = 5
REQ_PQ_MULTI = bytes.fromhex("f18e7ebe")
RES_PQ_CTOR = bytes.fromhex("63241605")  # resPQ(0x05162463) 在报文中的小端序列化


# --------------------------------------------------------------------------- #
# top40.json 拉取
# --------------------------------------------------------------------------- #

def _fetch_one(url: str, timeout: float) -> dict[str, Any]:
    """拉取单个镜像并做结构校验。失败抛异常。"""
    req = Request(url, headers={"User-Agent": "tg-sync-bot-proxy-selector/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    proxies = data.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise ValueError("top40.json 中 proxies 为空")
    for item in proxies[:1]:    # 抽查首条结构
        if not all(k in item for k in ("server", "port", "secret")):
            raise ValueError("top40.json 结构异常，缺少 server/port/secret 字段")
    return data


def fetch_top40(sources: list[str] | None = None,
                timeout: float = FETCH_TIMEOUT) -> tuple[list[dict], str]:
    """依次尝试镜像源，返回 (代理列表, 成功的源地址)。全部失败抛异常。"""
    errors: list[str] = []
    for url in (sources or DEFAULT_SOURCES):
        try:
            data = _fetch_one(url, timeout)
            proxies = [
                {
                    "server": str(p["server"]).strip(),
                    "port": int(p["port"]),
                    "secret": str(p["secret"]).strip(),
                    "latency_ms": p.get("latency_ms"),
                    "verified": bool(p.get("verified")),
                    "sources": p.get("sources") or [],
                }
                for p in data["proxies"]
                if p.get("server") and p.get("secret")
            ]
            if not proxies:
                raise ValueError("proxies 解析后为空")
            logger.info("已拉取 top40.json：%s（%d 条，榜单生成于 %s）",
                        url, len(proxies), data.get("generated_at", "?"))
            return proxies, url
        except Exception as exc:
            errors.append("%s -> %s: %s" % (url, type(exc).__name__, exc))
            logger.warning("镜像拉取失败: %s", errors[-1])
    raise RuntimeError("所有 top40.json 镜像均拉取失败:\n" + "\n".join(errors))


# --------------------------------------------------------------------------- #
# 本地测速与选优
# --------------------------------------------------------------------------- #

def _secret_kind(secret: str) -> str:
    from .mtproxy_probe import parse_secret
    return parse_secret(secret).kind


def probe_candidates(proxies: list[dict], *, concurrency: int = PROBE_CONCURRENCY,
                     attempts: int = PROBE_ATTEMPTS,
                     timeout: float = PROBE_TIMEOUT,
                     connect_timeout: float = PROBE_CONNECT_TIMEOUT,
                     progress: bool = False) -> list[dict]:
    """在本机对候选代理做真实握手测速并排序（最优在前）。"""
    targets = [
        (p["server"], p["port"], p["secret"], tuple(p.get("sources") or ()))
        for p in proxies
    ]

    # mtproxy_probe.probe_many 接收 Target 对象
    from .mtproxy_probe import Target
    results = asyncio.run(probe_many(
        [Target(*t) for t in targets],
        concurrency=concurrency, attempts=attempts,
        connect_timeout=connect_timeout, handshake_timeout=timeout,
        progress=progress,
    ))
    return rank_results(results)


def pick_best(ranked: list[dict]) -> dict | None:
    """从排序结果中选出最优代理。

    优先级：真实 DC 往返通过（按往返耗时）> 摘要校验通过 > 仅能握手。
    返回 dict（含 server/port/secret/kind/latency_ms/dc_ms 等），全失效返回 None。
    """
    for item in ranked:
        if item.get("dc_ok"):
            return _candidate_from(item)
    for item in ranked:
        if item.get("alive") and item.get("verified"):
            logger.warning("没有代理通过 DC 往返校验，回退到握手校验通过的代理")
            return _candidate_from(item)
    for item in ranked:
        if item.get("alive"):
            logger.warning("没有摘要校验通过的代理，回退到未校验但可握手的代理")
            return _candidate_from(item)
    return None


def _candidate_from(item: dict) -> dict:
    return {
        "server": item["server"],
        "port": item["port"],
        "secret": item["secret"],
        "kind": _secret_kind(item["secret"]),
        "latency_ms": item.get("latency_ms"),
        "dc_ms": item.get("dc_ms"),
        "dc_ok": bool(item.get("dc_ok")),
        "verified": bool(item.get("verified")),
        "link": item.get("link"),
    }


# --------------------------------------------------------------------------- #
# 真实 DC 往返校验
# --------------------------------------------------------------------------- #

def _quiet_loggers():
    """连接类需要一个 loggers 映射；这里统一指向静默 logger，避免刷屏。"""
    quiet = logging.getLogger("tg_forward_bot.proxy_dc")
    quiet.setLevel(logging.CRITICAL + 1)
    return collections.defaultdict(lambda: quiet)


async def _dc_roundtrip(candidate: dict, *,
                        timeout: float = DC_VALIDATE_TIMEOUT) -> float | None:
    """经代理向 Telegram DC 发一次明文 req_pq_multi，返回往返毫秒数。

    能拿到带匹配 nonce 的 resPQ 才说明这条代理真的能承载 MTProto 流量；
    握手成功但随后被断连的代理在此会返回 None。
    """
    from telethon.client.telegrambaseclient import (
        DEFAULT_DC_ID, DEFAULT_IPV4_IP, DEFAULT_PORT)

    try:
        conn_cls, proxy_tuple = build_telethon_proxy(candidate)
    except ValueError:
        return None

    conn = conn_cls(DEFAULT_IPV4_IP, DEFAULT_PORT, DEFAULT_DC_ID,
                    loggers=_quiet_loggers(), proxy=proxy_tuple)
    started = time.perf_counter()
    try:
        await asyncio.wait_for(conn.connect(), timeout)
        nonce = os.urandom(16)
        body = REQ_PQ_MULTI + nonce
        msg_id = int(time.time() * 2 ** 32) & ~3
        await conn.send(struct.pack("<qqi", 0, msg_id, len(body)) + body)
        resp = await asyncio.wait_for(conn.recv(), timeout)
        if len(resp) < 40 or resp[20:24] != RES_PQ_CTOR or resp[24:40] != nonce:
            return None
        return round((time.perf_counter() - started) * 1000, 1)
    except Exception as exc:
        logger.debug("DC 往返失败 %s:%s -> %r",
                     candidate.get("server"), candidate.get("port"), exc)
        return None
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


async def validate_candidates(ranked: list[dict], *,
                              top: int = DC_VALIDATE_TOP,
                              timeout: float = DC_VALIDATE_TIMEOUT,
                              concurrency: int = DC_VALIDATE_CONCURRENCY
                              ) -> list[dict]:
    """对握手排序前 ``top`` 名做 DC 往返校验，并把通过的按往返耗时排到最前。

    未通过校验的候选保留在原位（作为回退），其 ``dc_ok`` 为 False。
    """
    pool = [item for item in ranked if item.get("alive")][:max(1, top)]
    if not pool:
        return ranked

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item: dict) -> None:
        async with sem:
            item["dc_ms"] = await _dc_roundtrip(
                _candidate_from(item), timeout=timeout)
        item["dc_ok"] = item["dc_ms"] is not None

    await asyncio.gather(*(_one(item) for item in pool))

    ok = sorted((i for i in pool if i.get("dc_ok")), key=lambda i: i["dc_ms"])
    ok_ids = {id(i) for i in ok}
    # 未通过的候选必须留在列表里：全部失败时仍要能回退到握手排序的结果
    failed = [i for i in pool if id(i) not in ok_ids]
    pool_ids = {id(i) for i in pool}
    tail = [i for i in ranked if id(i) not in pool_ids]

    if ok:
        logger.info("DC 往返校验通过 %d/%d：%s", len(ok), len(pool),
                    ", ".join("%s:%d(%sms)" % (i["server"], i["port"], i["dc_ms"])
                              for i in ok))
    else:
        logger.warning("DC 往返校验无一通过（共测 %d 个），本轮回退到握手排序", len(pool))
    return ok + failed + tail


# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #

def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str = DEFAULT_STATE_FILE) -> dict | None:
    """读取当前生效的代理状态。文件缺失或损坏返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    candidate = state.get("current") or {}
    if not all(k in candidate for k in ("server", "port", "secret")):
        return None
    return state


def save_state(best: dict, ranked: list[dict], source_url: str,
               path: str = DEFAULT_STATE_FILE) -> dict:
    """把选优结果原子写入状态文件。"""
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_url,
        "current": best,
        "candidates": [
            {**{k: c[k] for k in ("server", "port", "secret", "kind",
                                  "latency_ms", "dc_ms", "dc_ok", "verified")
               if k in c},
             # alive 必须落盘：main.py 的复测逻辑靠它判断「当前代理是否仍可用」
             "alive": bool(c.get("alive", True))}
            for c in ranked[:DEFAULT_CANDIDATES]
        ],
    }
    _atomic_write_json(path, state)
    return state


# --------------------------------------------------------------------------- #
# 选优主流程
# --------------------------------------------------------------------------- #

def normalize_config(proxy_cfg: dict | None) -> dict:
    cfg = dict(proxy_cfg or {})
    cfg.setdefault("state_file", DEFAULT_STATE_FILE)
    cfg.setdefault("sources", [])
    probe = dict(cfg.get("probe") or {})
    probe.setdefault("concurrency", PROBE_CONCURRENCY)
    probe.setdefault("attempts", PROBE_ATTEMPTS)
    probe.setdefault("timeout", PROBE_TIMEOUT)
    probe.setdefault("connect_timeout", PROBE_CONNECT_TIMEOUT)
    probe.setdefault("dc_validate_top", DC_VALIDATE_TOP)
    probe.setdefault("dc_validate_timeout", DC_VALIDATE_TIMEOUT)
    cfg["probe"] = probe
    return cfg


async def _fetch_async(sources: list[str], timeout: float) -> tuple[list[dict], str]:
    return await asyncio.to_thread(fetch_top40, sources, timeout)


async def run_refresh(proxy_cfg: dict | None = None,
                      *, progress: bool = False) -> dict | None:
    """执行一轮「拉取 → 测速 → 选优 → 写状态」。

    返回新的状态 dict；拉取失败或所有代理失效时返回 None（旧状态保持不动）。
    """
    cfg = normalize_config(proxy_cfg)
    try:
        proxies, source_url = await _fetch_async(
            cfg["sources"] or None, FETCH_TIMEOUT)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return None

    probe = cfg["probe"]
    ranked = await asyncio.to_thread(
        probe_candidates, proxies,
        concurrency=probe["concurrency"], attempts=probe["attempts"],
        timeout=probe["timeout"], connect_timeout=probe["connect_timeout"],
        progress=progress,
    )

    # 握手排序只是初筛：再对前 N 名做一次真实 DC 往返，避免选中「能握手但传不了数据」的代理
    ranked = await validate_candidates(
        ranked, top=int(probe["dc_validate_top"]),
        timeout=float(probe["dc_validate_timeout"]))

    best = pick_best(ranked)
    if best is None:
        logger.error("本轮测速全部代理失效，保持现有代理配置不变")
        return None

    state = save_state(best, ranked, source_url, cfg["state_file"])
    logger.info(
        "代理选优完成：%s:%d（%s，握手 %sms，DC 往返 %sms，校验%s）→ %s",
        best["server"], best["port"], best["kind"], best["latency_ms"],
        best.get("dc_ms") if best.get("dc_ms") is not None else "未通过",
        "通过" if best["verified"] else "未通过", cfg["state_file"])
    return state


# --------------------------------------------------------------------------- #
# Telethon 集成
# --------------------------------------------------------------------------- #

def build_telethon_proxy(candidate: dict) -> tuple[type, tuple]:
    """把候选代理转成 Telethon 的 (connection 类, proxy 元组)。

    * 0xEE（FakeTLS）→ 自实现的 ConnectionTcpMTProxyFakeTLS
    * 0xDD → 内置 ConnectionTcpMTProxyRandomizedIntermediate
    * 裸 16 字节 → 内置 ConnectionTcpMTProxyAbridged
    """
    kind = candidate.get("kind") or _secret_kind(candidate["secret"])
    secret = candidate["secret"]
    proxy_tuple = (candidate["server"], int(candidate["port"]), secret)

    if kind == "faketls":
        from .faketls_mtproxy import ConnectionTcpMTProxyFakeTLS
        return ConnectionTcpMTProxyFakeTLS, proxy_tuple
    if kind == "obfuscated":
        from telethon.network.connection import (
            ConnectionTcpMTProxyRandomizedIntermediate)
        return ConnectionTcpMTProxyRandomizedIntermediate, proxy_tuple
    if kind == "plain":
        from telethon.network.connection import ConnectionTcpMTProxyAbridged
        return ConnectionTcpMTProxyAbridged, proxy_tuple
    raise ValueError("不支持的 secret 类型: %s" % kind)


def state_to_candidate(state: dict | None) -> dict | None:
    if not state:
        return None
    return state.get("current")


def build_client_kwargs(proxy) -> dict:
    """把 ``(connection 类, proxy 元组)`` 转成 ``TelegramClient`` 的构造参数。"""
    if not proxy:
        return {}
    conn_cls, proxy_tuple = proxy
    return {"connection": conn_cls, "proxy": proxy_tuple}


def verify_proxy_applied(client, label: str) -> bool:
    """校验某个客户端确实挂上了代理，避免"以为走了代理其实在直连"。

    返回 True 表示代理已生效。无法直连 Telegram 的网络里，一旦返回 False，
    该客户端的每一次请求都会超时——所以这里用 CRITICAL 级别告警。
    """
    conn_cls = getattr(client, "_connection", None)
    proxy_tuple = getattr(client, "_proxy", None)
    if proxy_tuple is None or conn_cls is None:
        logger.critical(
            "%s 未挂代理：该客户端会直连 Telegram。若本机无法直连，请检查 "
            "config.yaml 的 proxy 配置是否生效。", label)
        return False

    # 已建立连接时进一步核对实际使用的传输类（能抓到"参数被中间层覆盖"这类问题）
    sender = getattr(client, "_sender", None)
    live_conn = getattr(sender, "connection", None) if sender else None
    if live_conn is not None and type(live_conn) is not conn_cls:
        logger.critical(
            "%s 实际连接类为 %s，与配置的 %s 不一致，可能绕过代理！",
            label, type(live_conn).__name__, conn_cls.__name__)
        return False

    logger.debug("%s 代理已生效：%s（连接类 %s）",
                 label, proxy_tuple[0], conn_cls.__name__)
    return True


def create_client(session, api_id, api_hash, *, proxy=None, **kwargs):
    """构造 ``TelegramClient`` 的唯一入口。

    所有需要访问 Telegram 的客户端都必须经由此函数创建，代理参数在这里统一注入，
    避免新代码忘记传 ``connection``/``proxy`` 而在无法直连的网络中静默失败。
    """
    from telethon import TelegramClient

    kwargs.update(build_client_kwargs(proxy))
    if proxy is None:
        return TelegramClient(session, api_id, api_hash, **kwargs)

    # MTProxy 由 connection 类自身实现（不使用 python-socks），但 Telethon 对任何
    # proxy 参数都会告警「proxy argument will be ignored」，容易在排障时误导。
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".*proxy argument will be ignored.*")
        client = TelegramClient(session, api_id, api_hash, **kwargs)

    verify_proxy_applied(client, "TelegramClient(%s)" % session)
    return client


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli() -> int:
    parser = argparse.ArgumentParser(description="MTProto 代理自动选优")
    parser.add_argument("--once", action="store_true",
                        help="执行一轮选优并写入状态文件")
    parser.add_argument("--test", action="store_true",
                        help="只测速打印榜单，不写状态文件")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--sources", nargs="*", default=None,
                        help="覆盖 top40.json 镜像地址列表")
    parser.add_argument("--concurrency", type=int, default=PROBE_CONCURRENCY)
    parser.add_argument("--attempts", type=int, default=PROBE_ATTEMPTS)
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT)
    parser.add_argument("--no-dc", action="store_true",
                        help="--test 时跳过 DC 往返校验（只做握手测速）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.test:
        proxies, source_url = fetch_top40(args.sources, FETCH_TIMEOUT)
        ranked = probe_candidates(
            proxies, concurrency=args.concurrency,
            attempts=args.attempts, timeout=args.timeout, progress=True)
        if not args.no_dc:
            ranked = asyncio.run(validate_candidates(ranked))
        print(json.dumps({
            "source": source_url,
            "tested": len(ranked),
            "alive": sum(1 for r in ranked if r["alive"]),
            "verified": sum(1 for r in ranked if r["verified"]),
            "dc_ok": sum(1 for r in ranked if r.get("dc_ok")),
            "top10": [
                {k: r.get(k) for k in ("rank", "server", "port", "verified",
                                       "latency_ms", "alive", "dc_ms", "dc_ok")}
                for r in ranked[:10]
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.once:
        cfg = {"state_file": args.state_file, "sources": args.sources or []}
        state = asyncio.run(run_refresh(cfg, progress=True))
        return 0 if state else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
