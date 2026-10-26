"""代理测速命令后端：拉取榜单 → 并发握手测速 → DC 往返校验 → 选优落盘。

数据源是 telegram-mtproto-proxy-list 仓库的两份榜单：
- proxies.json：全量代理（约 1000 条，数组格式，字段在 ``fields`` 里）
- top40.json：榜单前 40 名（字典格式）

两条测速命令共用 ``run_speed_test``；测速完成后自动把最优代理写入
状态文件（data/proxy.json），与启动选优共用同一套「写状态 → 重启生效」机制。
"""

from __future__ import annotations

import asyncio
import logging
import time
from json import loads as _json_loads
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from .mtproxy_probe import Target, probe_target
from .proxy_manager import (
    DEFAULT_SOURCES,
    FETCH_TIMEOUT,
    REPO,
    normalize_config,
    pick_best,
    rank_results,
    save_state,
    validate_candidates,
)

logger = logging.getLogger(__name__)

ALL = "all"        # proxies.json 全量
TOP40 = "top40"    # top40.json 前 40 名

# 每份榜单在镜像源上的文件名；sources 配置里的 URL 都指向 top40.json，
# 全量榜单一律按同名规则推导（top40.json -> proxies.json）
_FILENAMES = {ALL: "proxies.json", TOP40: "top40.json"}

ProgressCb = Callable[[int, int, int], Awaitable[None]]


def derive_urls(kind: str, sources: list[str] | None = None) -> list[str]:
    """按榜单类型推导镜像 URL 列表。

    配置的 ``proxy.sources``（及内置镜像）都指向 top40.json；
    全量榜单把文件名替换成 proxies.json，其余路径保持不变。
    """
    filename = _FILENAMES[kind]
    base = list(sources or DEFAULT_SOURCES)
    if not base:
        base = ["https://raw.githubusercontent.com/%s/main/top40.json" % REPO]
    urls = []
    for url in base:
        if url.rsplit("/", 1)[-1] == filename:
            urls.append(url)
        else:
            urls.append(url[: url.rfind("/")] + "/" + filename)
    return urls


def parse_proxy_payload(data: Any) -> list[dict]:
    """解析两种榜单格式，去重后返回统一结构的代理列表。

    - top40.json：{"proxies": [{"server": ..., "port": ..., "secret": ...}, ...]}
    - proxies.json：{"fields": [...], "proxies": [[v1, v2, ...], ...]}
    """
    if not isinstance(data, dict):
        raise ValueError("榜单根节点不是对象")
    raw = data.get("proxies")
    if not isinstance(raw, list) or not raw:
        raise ValueError("榜单中 proxies 为空")

    items: list[dict]
    if raw and isinstance(raw[0], dict):
        items = raw
    elif raw and isinstance(raw[0], list):
        fields = data.get("fields")
        if not isinstance(fields, list):
            raise ValueError("数组格式榜单缺少 fields 字段")
        try:
            idx = {name: i for i, name in enumerate(fields)}
            items = [
                {name: row[i] for name, i in idx.items() if i < len(row)}
                for row in raw
            ]
        except (TypeError, KeyError) as exc:
            raise ValueError("数组格式榜单解析失败: %r" % exc) from exc
    else:
        raise ValueError("proxies 元素类型异常: %r" % type(raw[0]).__name__)

    proxies: list[dict] = []
    seen: set[tuple] = set()
    for p in items:
        if not isinstance(p, dict):
            continue
        server = str(p.get("server") or "").strip()
        secret = str(p.get("secret") or "").strip()
        try:
            port = int(p.get("port"))
        except (TypeError, ValueError):
            continue
        if not server or not secret:
            continue
        key = (server, port, secret)
        if key in seen:
            continue
        seen.add(key)
        proxies.append({
            "server": server,
            "port": port,
            "secret": secret,
            "latency_ms": p.get("latency_ms"),
            "verified": bool(p.get("verified")),
            "sources": p.get("sources") or [],
        })
    if not proxies:
        raise ValueError("榜单解析后没有有效代理")
    return proxies


async def _fetch_one(url: str, timeout: float) -> list[dict]:
    def _read() -> list[dict]:
        req = Request(url, headers={"User-Agent": "tg-sync-bot-proxy-speedtest/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return parse_proxy_payload(_json_loads(resp.read().decode("utf-8")))

    return await asyncio.to_thread(_read)


async def fetch_proxy_list(kind: str, sources: list[str] | None = None,
                           timeout: float = FETCH_TIMEOUT) -> tuple[list[dict], str]:
    """依次尝试镜像源拉取指定榜单。全部失败抛 RuntimeError。"""
    errors: list[str] = []
    for url in derive_urls(kind, sources):
        try:
            proxies = await _fetch_one(url, timeout)
            logger.info("已拉取 %s：%s（%d 条）", kind, url, len(proxies))
            return proxies, url
        except Exception as exc:
            errors.append("%s -> %s: %s" % (url, type(exc).__name__, exc))
            logger.warning("榜单拉取失败: %s", errors[-1])
    raise RuntimeError("所有镜像均拉取失败:\n" + "\n".join(errors))


async def _probe_all(proxies: list[dict], *, concurrency: int, attempts: int,
                     connect_timeout: float, handshake_timeout: float,
                     progress_cb: ProgressCb | None) -> list[dict]:
    """并发探测全部代理；单台异常（超时/不可用/坏密钥）只记失败不中断。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(proxies)
    done = 0
    results: list[dict] = []
    last_report = 0.0

    async def _maybe_report(force: bool = False) -> None:
        nonlocal last_report
        if progress_cb is None:
            return
        now = time.monotonic()
        if force or (now - last_report >= 10.0):
            last_report = now
            alive = sum(1 for r in results if r.get("alive"))
            try:
                await progress_cb(done, total, alive)
            except Exception as exc:
                logger.debug("测速进度回调失败: %s", exc)

    async def _one(p: dict) -> None:
        nonlocal done
        async with sem:
            try:
                result = await probe_target(
                    Target(p["server"], p["port"], p["secret"],
                           tuple(p.get("sources") or ())),
                    attempts=attempts,
                    connect_timeout=connect_timeout,
                    handshake_timeout=handshake_timeout,
                )
            except Exception as exc:  # 超时/连接拒绝/其它异常 → 该台计为不可用
                result = {
                    "server": p["server"], "port": p["port"], "secret": p["secret"],
                    "sources": list(p.get("sources") or ()),
                    "alive": False, "verified": False, "mode": "error",
                    "latency_ms": None, "connect_ms": None, "tls_ms": None,
                    "success_rate": 0.0, "samples": 0,
                    "error": "probe_exception:%s" % type(exc).__name__,
                }
        results.append(result)
        done += 1
        await _maybe_report()

    await asyncio.gather(*(_one(p) for p in proxies))
    await _maybe_report(force=True)
    return results


async def run_speed_test(kind: str, proxy_cfg: dict | None = None,
                         *, progress_cb: ProgressCb | None = None) -> dict:
    """执行一轮「拉取 → 测速 → DC 校验 → 选优 → 写状态」。

    返回报告 dict；拉取失败抛 RuntimeError，全部代理不可用同样抛 RuntimeError
    （旧状态保持不动）。写状态成功即视为「已设置为当前使用的代理」，
    重启进程后按新状态连接。
    """
    cfg = normalize_config(proxy_cfg)
    probe = cfg["probe"]
    started = time.monotonic()

    proxies, source_url = await fetch_proxy_list(
        kind, cfg["sources"] or None, FETCH_TIMEOUT)
    if progress_cb is not None:
        try:
            await progress_cb(0, len(proxies), 0)
        except Exception:
            pass

    results = await _probe_all(
        proxies,
        concurrency=int(probe["concurrency"]),
        attempts=int(probe["attempts"]),
        connect_timeout=float(probe["connect_timeout"]),
        handshake_timeout=float(probe["timeout"]),
        progress_cb=progress_cb,
    )

    alive_count = sum(1 for r in results if r.get("alive"))
    ranked = await validate_candidates(
        rank_results(results),
        top=int(probe["dc_validate_top"]),
        timeout=float(probe["dc_validate_timeout"]))
    best = pick_best(ranked)
    if best is None:
        raise RuntimeError(
            "%d 条代理全部不可用（握手均失败），当前代理保持不变" % len(proxies))

    state = save_state(best, ranked, source_url, cfg["state_file"])
    elapsed = time.monotonic() - started
    dc_ok = sum(1 for c in state.get("candidates", []) if c.get("dc_ok"))
    logger.info("测速完成 kind=%s 共 %d 条 存活 %d 最优 %s:%s dc_ms=%s 用时 %.1fs",
                kind, len(proxies), alive_count, best["server"], best["port"],
                best.get("dc_ms"), elapsed)
    return {
        "kind": kind,
        "total": len(proxies),
        "alive": alive_count,
        "failed": len(proxies) - alive_count,
        "best": best,
        "ranked": ranked,
        "state_file": cfg["state_file"],
        "source_url": source_url,
        "elapsed": elapsed,
        "dc_ok": dc_ok,
    }
