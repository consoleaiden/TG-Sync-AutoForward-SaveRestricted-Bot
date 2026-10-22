#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出网审计：验证所有 Telegram 访问都经过 MTProto 代理，没有偷偷直连。

原理
----
在建立任何客户端**之前**劫持底层 TCP 建连（``asyncio.open_connection``），
记录每一次真实连接的目标地址；然后按功能链路逐个触发请求（登录态查询、
链接解析、拉取历史、Takeout 导出、跨 DC 导出连接、媒体下载、UserBot 会话），
最后核对：**所有连接目标必须等于代理服务器**，且不得出现 Telegram 官方 IP 段。

用法::

    docker compose run --rm bot python scripts/audit_proxy_egress.py
    docker compose run --rm bot python scripts/audit_proxy_egress.py --skip-media
    docker compose run --rm bot python scripts/audit_proxy_egress.py --json

退出码：0=全部经过代理；1=发现直连；2=环境/配置问题无法审计。
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import ipaddress
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from telethon import errors  # noqa: E402

from core import proxy_manager as pm  # noqa: E402
from core.fast_telethon import fast_download_file  # noqa: E402
from core.keepalive import install as install_keepalive  # noqa: E402

# Telegram 官方网段（出现即视为直连）
TELEGRAM_NETS = [ipaddress.ip_network(n) for n in (
    "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22", "91.108.16.0/22",
    "91.108.56.0/22", "95.161.64.0/20", "149.154.160.0/20", "149.154.164.0/22",
    "2001:67c:4e8::/48", "2001:b28:f23d::/48", "2001:b28:f23f::/48",
)]

RECORDS: collections.Counter = collections.Counter()
_orig_open_connection = asyncio.open_connection


async def _recording_open_connection(host=None, port=None, *args, **kwargs):
    try:
        RECORDS[(str(host), int(port))] += 1
    except (TypeError, ValueError):
        RECORDS[(str(host), str(port))] += 1
    return await _orig_open_connection(host, port, *args, **kwargs)


def _is_telegram_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in TELEGRAM_NETS if net.version == addr.version)


async def _step(name: str, coro, results: list, timeout: float = 30.0):
    """执行一个审计步骤，记录成功/跳过/失败，但不中断整轮审计。"""
    try:
        detail = await asyncio.wait_for(coro, timeout)
        results.append({"step": name, "status": "ok", "detail": detail or ""})
        print("  ✅ %-34s %s" % (name, detail or ""), flush=True)
    except Exception as exc:  # noqa: BLE001 - 审计需要继续跑完
        results.append({"step": name, "status": "fail",
                        "detail": "%s: %s" % (type(exc).__name__, exc)})
        print("  ⚠️  %-34s %s: %s" % (name, type(exc).__name__, exc), flush=True)


async def audit_bot(config: dict, candidate: dict, results: list) -> None:

    proxy = pm.build_telethon_proxy(candidate)
    client = pm.create_client("sessions/bot", config["api_id"], config["api_hash"],
                              proxy=proxy, connection_retries=2, retry_delay=2,
                              timeout=30)
    await client.connect()
    try:
        print("• Bot 客户端（转发主力）", flush=True)
        if not await client.is_user_authorized():
            await client.start(bot_token=config["bot_token"])
        else:
            pm.verify_proxy_applied(client, "Bot")

        async def get_me():
            me = await client.get_me()
            return "id=%s @%s" % (me.id, getattr(me, "username", ""))
        await _step("登录态查询 get_me", get_me(), results)

        target = (config.get("audit_target") or "telegram")

        async def resolve():
            ent = await client.get_entity(target)
            return "解析 %s → %s" % (target, type(ent).__name__)
        await _step("链接解析 get_entity", resolve(), results, timeout=40)

        print("  连接类 = %s，proxy = %s:%s" % (
            client._connection.__name__, client._proxy[0], client._proxy[1]),
            flush=True)
        print("  说明：拉历史 / Takeout / 媒体下载 由 UserBot 执行，见下一节",
              flush=True)
    finally:
        await client.disconnect()


async def audit_userbots(config: dict, candidate: dict, results: list, *,
                         skip_media: bool, fast: bool = False) -> None:
    proxy = pm.build_telethon_proxy(candidate)
    phones = config.get("phones") or ([config["phone"]] if config.get("phone") else [])
    from core.userbot_manager import UserBotManager
    target = (config.get("audit_target") or "telegram")
    for idx, phone in enumerate(phones, 1):
        session_path = UserBotManager.get_session_path(phone, is_first=(idx == 1))
        print("• UserBot #%d（拉历史 / 受限消息 / 下载重传 的执行者）" % idx, flush=True)
        client = pm.create_client(session_path, config["api_id"], config["api_hash"],
                                  proxy=proxy, connection_retries=2, retry_delay=2,
                                  timeout=30)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                results.append({"step": "UserBot #%d" % idx, "status": "skip",
                                "detail": "未登录，跳过"})
                print("  ⚠️  %-34s 未登录，跳过" % ("UserBot #%d" % idx), flush=True)
                continue
            pm.verify_proxy_applied(client, "UserBot #%d" % idx)

            async def dialogs():
                ds = await client.get_dialogs(limit=5)
                return "%d 个会话" % len(ds)
            await _step("UserBot #%d 会话列表" % idx, dialogs(), results, timeout=40)

            async def read_history():
                msgs = await client.get_messages(target, limit=5)
                return "%d 条历史消息" % len([m for m in msgs if m])
            await _step("UserBot #%d 历史同步" % idx, read_history(), results,
                        timeout=60)

            async def takeout_probe():
                try:
                    async with client.takeout() as takeout:
                        me = await takeout.get_me()
                        return "Takeout 内 get_me id=%s" % me.id
                except errors.TakeoutInitDelayError as exc:
                    # 账号新建/刚导出过时 Telegram 要求等待，属于服务端限流而非代理问题
                    return "跳过（Telegram 要求等待 %ss）" % exc.seconds
            await _step("UserBot #%d 受限消息 Takeout" % idx, takeout_probe(),
                        results, timeout=90)

            async def exported_sender():
                # FastTelethon 下载/上传走的就是这条路径：新建连接 + 导出授权
                other_dc = 4 if client.session.dc_id != 4 else 2
                sender = await client._borrow_exported_sender(other_dc)
                try:
                    return "跨 DC%d 借调 sender 成功" % other_dc
                finally:
                    await client._return_exported_sender(sender)
            await _step("UserBot #%d 跨 DC 多连接" % idx, exported_sender(),
                        results, timeout=60)

            if not skip_media:
                async def download_media():
                    from telethon.tl import types
                    real = (types.MessageMediaPhoto, types.MessageMediaDocument)
                    async for msg in client.iter_messages(target, limit=60):
                        media = getattr(msg, "media", None)
                        if not isinstance(media, real):
                            continue    # 跳过链接预览（MessageMediaWebPage）等无实体文件
                        path = os.path.join(tempfile.gettempdir(),
                                            "audit_media_%s" % msg.id)
                        got = await client.download_media(msg, file=path)
                        if not (got and os.path.exists(got)):
                            return "该消息无可下载媒体"
                        size = os.path.getsize(got)
                        os.unlink(got)
                        return "下载媒体 %d 字节" % size
                    return "未找到带实体媒体的消息（跳过）"
                await _step("UserBot #%d 媒体下载" % idx, download_media(), results,
                            timeout=150)

            if fast:
                async def multi_connections():
                    # 与 FastTelethon 完全相同的分支：同 DC 手工构造 4 个 sender
                    from core.fast_telethon import _acquire_senders, _release_senders
                    dc_id = client.session.dc_id
                    senders, borrowed = await _acquire_senders(client, dc_id, 4)
                    try:
                        return "%d 条并发连接建立成功（同 DC%s）" % (len(senders), dc_id)
                    finally:
                        await _release_senders(client, senders, borrowed)
                await _step("UserBot #%d 4 连接并发" % idx, multi_connections(),
                            results, timeout=90)

            print("  连接类 = %s，proxy = %s:%s" % (
                client._connection.__name__, client._proxy[0], client._proxy[1]),
                flush=True)
        finally:
            await client.disconnect()


def _judge(candidate: dict) -> tuple[bool, list[dict]]:
    proxy_host = str(candidate["server"])
    proxy_port = int(candidate["port"])
    rows, ok = [], True
    for (host, port), count in RECORDS.most_common():
        if host == proxy_host and port == proxy_port:
            verdict, status = "代理", "ok"
        elif _is_telegram_ip(host):
            verdict, status, ok = "直连 Telegram！", "violation", False
        else:
            verdict, status, ok = "非代理目标", "warn", False
        rows.append({"target": "%s:%s" % (host, port), "count": count,
                     "verdict": verdict, "status": status})
    return ok, rows


async def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Telegram 访问是否全部经过代理")
    parser.add_argument("--skip-media", action="store_true", help="跳过媒体下载步骤")
    parser.add_argument("--fast", action="store_true",
                        help="额外用 4 连接并发下载，验证多连接加速也走代理")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if not os.path.exists(args.config):
        print("❌ 找不到 %s" % args.config, file=sys.stderr)
        return 2
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    cfg = pm.normalize_config(config.get("proxy"))
    mode = str(cfg.get("mode") or "off").lower()
    if mode == "off":
        print("❌ proxy.mode=off，当前为直连模式，无需审计（也无法在受限网络使用）",
              file=sys.stderr)
        return 2

    if mode == "manual":
        m = cfg.get("manual") or {}
        candidate = {"server": str(m["server"]), "port": int(m["port"]),
                     "secret": str(m["secret"])}
    else:
        state = pm.load_state(cfg["state_file"])
        if not state:
            print("🔄 尚无代理状态，先执行一轮选优...", flush=True)
            state = await pm.run_refresh(cfg, progress=True)
        if not state:
            print("❌ 代理选优失败，无法审计", file=sys.stderr)
            return 2
        candidate = pm.state_to_candidate(state)

    print("审计目标代理：%s:%s (%s)" % (candidate["server"], candidate["port"],
                                 candidate.get("kind") or "?"), flush=True)
    print("=" * 64, flush=True)

    install_keepalive(cfg.get("keepalive_seconds", 20))
    asyncio.open_connection = _recording_open_connection

    results: list[dict] = []
    try:
        await audit_bot(config, candidate, results)
        await audit_userbots(config, candidate, results,
                             skip_media=args.skip_media, fast=args.fast)
    finally:
        asyncio.open_connection = _orig_open_connection

    print("=" * 64, flush=True)
    print("=== TCP 建连目标统计 ===", flush=True)
    ok, rows = _judge(candidate)
    for row in rows:
        mark = {"ok": "✅", "warn": "⚠️", "violation": "❌"}[row["status"]]
        print("  %s %-28s %4d 次   %s" % (mark, row["target"], row["count"],
                                          row["verdict"]), flush=True)
    if not rows:
        print("  （没有任何建连记录）", flush=True)

    total = sum(RECORDS.values())
    print("=" * 64, flush=True)
    if ok and total:
        print("✅ PASS：%d 次建连全部指向代理，未发现直连 Telegram 的行为" % total,
              flush=True)
    elif not total:
        print("⚠️  未捕获到任何建连，请检查是否提前建立了连接（结果不可信）", flush=True)
    else:
        print("❌ FAIL：存在未经过代理的建连，请查看上表中标记为 ❌/⚠️ 的目标",
              flush=True)

    if args.json:
        print(json.dumps({"proxy": candidate, "records": rows, "steps": results,
                          "pass": bool(ok and total)}, ensure_ascii=False, indent=2))
    return 0 if (ok and total) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
