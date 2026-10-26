import os
import sys
import asyncio
import logging
import yaml
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from db.database import Database
from bot.handlers import register_handlers
from core.config_store import apply_overlay, async_load_overlay
from core.monitor import MonitorManager
from core.storage import (
    audit_db_path,
    format_audit,
    persistence_warning,
    resolve_db_path,
    wal_enabled,
)
from core.userbot_manager import UserBotManager, UserBotNotAuthorized
from core.keepalive import install as install_keepalive
from core.proxy_manager import (
    load_state,
    normalize_config,
    run_refresh,
    state_to_candidate,
    build_telethon_proxy,
    create_client,
    verify_proxy_applied,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tg_forward_bot")

# ---------------------------------------------------------------------------
# 重连策略常量
# ---------------------------------------------------------------------------
MAX_RECONNECT_ATTEMPTS = 20      # 应用层最多重连次数 (20 × 30min = 10h)
RECONNECT_BASE_DELAY = 1800      # 重连间隔 (秒) = 30 分钟
RECONNECT_MAX_DELAY = 1800       # 最大间隔 (秒) = 30 分钟


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# MTProto 代理支持（无法直连 Telegram 的网络环境）
# ---------------------------------------------------------------------------
def _candidate_key(candidate: dict) -> tuple:
    return (candidate.get("server"), candidate.get("port"),
            candidate.get("secret"))


def _latency_metric(candidate: dict) -> float:
    """用于比较代理优劣的耗时指标：优先真实 DC 往返，其次握手耗时。"""
    for key in ("dc_ms", "latency_ms"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 10 ** 9


def _state_age_minutes(state: dict) -> float:
    """状态文件的更新时间距今多少分钟；缺失/解析失败视为无限旧。"""
    try:
        t = datetime.fromisoformat(state.get("updated_at"))
    except (TypeError, ValueError):
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


async def _resolve_proxy(config: dict) -> tuple | None:
    """根据 config 中的 proxy 配置解析出 (connection 类, proxy 元组)。

    * mode: off    → 直连，返回 None
    * mode: manual → 使用 proxy.manual 中的固定代理
    * mode: auto   → 读取状态文件；没有则立即执行一轮选优（最多重试 3 次）

    自动选优模式下无法选出可用代理时直接退出进程，交由 Docker 重启策略
    拉起重试，避免在无代理状态下无限空转。
    """
    cfg = normalize_config(config.get("proxy"))
    mode = str(cfg.get("mode") or "off").lower()

    if mode == "off":
        return None

    if mode == "manual":
        m = cfg.get("manual") or {}
        if not (m.get("server") and m.get("port") and m.get("secret")):
            logger.critical("proxy.mode=manual 但 manual.server/port/secret 未配置完整")
            sys.exit(1)
        candidate = {
            "server": str(m["server"]), "port": int(m["port"]),
            "secret": str(m["secret"]),
        }
        return build_telethon_proxy(candidate)

    if mode != "auto":
        logger.critical("未知 proxy.mode: %s（可选 off/auto/manual）", mode)
        sys.exit(1)

    state = load_state(cfg["state_file"])
    # 状态过期就不沿用：代理是易逝资源，用几小时前的选优结果连接，
    # 大概率撞上已失效的代理（曾导致启动崩溃循环）
    stale_minutes = max(1, int(cfg.get("stale_minutes", 30)))
    if state is not None and _state_age_minutes(state) > stale_minutes:
        cur = state_to_candidate(state)
        logger.warning("已选代理 %s:%d 的状态已过期（更新于 %s，阈值 %d 分钟），重新选优",
                       cur.get("server"), cur.get("port"),
                       state.get("updated_at", "?"), stale_minutes)
        state = None
    if state is None:
        logger.info("未找到可用代理状态，开始执行代理选优...")
        for attempt in range(1, 4):
            state = await run_refresh(cfg)
            if state:
                break
            logger.warning("代理选优失败（第 %d/3 次），10 秒后重试...", attempt)
            await asyncio.sleep(10)
        if state is None:
            logger.critical("代理选优连续失败，进程退出（等待 Docker 重启后重试）")
            sys.exit(1)
    else:
        cur = state_to_candidate(state)
        logger.info("沿用已选代理：%s:%d（更新于 %s）",
                    cur.get("server"), cur.get("port"),
                    state.get("updated_at", "?"))

    return build_telethon_proxy(state_to_candidate(state))


async def _proxy_refresher(config: dict, current: dict | None) -> None:
    """每小时复测一次代理榜单，代理失效或明显更优时重启进程切换。"""
    cfg = normalize_config(config.get("proxy"))
    interval = max(5, int(cfg.get("refresh_minutes", 60))) * 60
    ratio = float(cfg.get("switch_improve_ratio", 0.7))

    while True:
        await asyncio.sleep(interval)
        try:
            state = await run_refresh(cfg)
        except Exception as exc:
            logger.error("代理复测异常（忽略，等待下轮）: %s", exc)
            continue
        if not state:
            continue

        new_best = state_to_candidate(state)
        if current is None:
            logger.warning("已选出可用代理 %s:%d，重启进程以应用",
                           new_best["server"], new_best["port"])
            os._exit(0)

        if _candidate_key(new_best) == _candidate_key(current):
            logger.info("代理复测完成，当前代理仍是最优，无需切换")
            continue

        # 当前代理是否仍在最新榜单中可用
        still_alive = False
        for c in state.get("candidates") or []:
            if (_candidate_key(c) == _candidate_key(current)
                    and c.get("alive") and c.get("verified")):
                still_alive = True
                break

        new_latency = _latency_metric(new_best)
        cur_latency = _latency_metric(current)
        if not still_alive:
            reason = "当前代理已失效"
        elif new_latency < cur_latency * ratio:
            reason = "发现明显更快的代理（%sms → %sms）" % (
                cur_latency, new_latency)
        else:
            logger.info("当前代理仍可用（%sms），最优代理 %sms 提升不足 %d%%，"
                        "保持不变", cur_latency, new_latency,
                        int((1 - ratio) * 100))
            continue

        logger.warning("%s，切换到 %s:%d，重启进程以应用新代理",
                       reason, new_best["server"], new_best["port"])
        os._exit(0)   # 由 Docker restart 策略拉起，启动时加载新代理


async def _reconnect_proxy_check(fail_streak: int, config: dict,
                                 current: dict | None) -> None:
    """重连连续失败时尝试重新选优；确认代理变更或多次无效则退出重启。"""
    cfg = normalize_config(config.get("proxy"))
    if str(cfg.get("mode") or "off").lower() != "auto":
        return
    if fail_streak % 3 != 0:
        return
    logger.warning("连续 %d 次重连失败，重新执行代理选优...", fail_streak)
    try:
        state = await run_refresh(cfg)
    except Exception as exc:
        logger.error("重连期间的代理选优失败: %s", exc)
        return
    if state is None:
        if fail_streak >= 9:
            logger.critical("重连与代理选优均持续失败，退出进程等待 Docker 重启")
            sys.exit(1)
        return
    new_best = state_to_candidate(state)
    if current is None or _candidate_key(new_best) != _candidate_key(current):
        logger.warning("代理已变更（%s:%d），重启进程以应用",
                       new_best["server"], new_best["port"])
        os._exit(0)
    elif fail_streak >= 9:
        logger.critical("代理未变更但持续无法连接，退出进程等待 Docker 重启")
        sys.exit(1)


async def _start_clients(bot: TelegramClient, userbot_manager: UserBotManager,
                         config: dict) -> None:
    """启动 Bot 和所有 UserBot 客户端。"""
    await bot.start(bot_token=config["bot_token"])
    logger.info("Bot 客户端已启动")

    await userbot_manager.start()


async def _boot_start_clients(bot: TelegramClient,
                              userbot_manager: UserBotManager,
                              db: Database, config: dict,
                              proxy_candidate: dict | None,
                              *, max_attempts: int = 3) -> None:
    """首次启动连接：失败不直接崩溃，先重新选代理再重试。

    - auto 模式下连接失败会立即重新选优；选出与当前不同的代理时
      重启进程应用（Docker restart 拉起后走新代理）；
    - 选优结果未变化（整批代理都不可用）则退避后原地重试连接，
      避免快速崩溃循环空耗 Docker 重启次数；
    - UserBotNotAuthorized 属于配置/部署问题（需人工登录），向上抛出。
    """
    cfg = normalize_config(config.get("proxy"))
    boot_auto = str(cfg.get("mode") or "off").lower() == "auto"
    for attempt in range(1, max_attempts + 1):
        try:
            await _start_clients(bot, userbot_manager, config)
            return
        except (ConnectionError, OSError, TimeoutError) as e:
            logger.warning("启动连接失败（第 %d/%d 次）: %s",
                           attempt, max_attempts, e)
            if attempt >= max_attempts:
                logger.critical("启动连接连续 %d 次失败，进程退出（等待 Docker 重启）",
                                attempt)
                await db.close()
                sys.exit(1)
            if boot_auto:
                logger.warning("重新执行代理选优…")
                try:
                    state = await run_refresh(cfg)
                except Exception as exc:
                    logger.error("代理选优失败: %s", exc)
                    state = None
                nb = state_to_candidate(state) if state else None
                if nb and (proxy_candidate is None or
                           _candidate_key(nb) != _candidate_key(proxy_candidate)):
                    logger.warning("已选出新代理 %s:%d，重启进程以应用",
                                   nb["server"], nb["port"])
                    await db.close()
                    os._exit(0)
                logger.warning("代理选优结果未变化（可能整批代理都不可用），退避后原地重试")
            await asyncio.sleep(min(15 * attempt, 60))


async def _setup_bot(bot: TelegramClient, userbot_manager: UserBotManager,
                     db: Database, config: dict) -> MonitorManager:
    """注册命令菜单、处理器，恢复任务。返回 MonitorManager 实例。"""
    # 设置 Bot 命令菜单（覆盖旧命令）
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="打开菜单（推荐）"),
            BotCommand(command="kl", description="克隆频道全部消息并持续监听"),
            BotCommand(command="kljx", description="继续克隆：可带起始ID或区间"),
            BotCommand(command="jt", description="只监听频道新消息"),
            BotCommand(command="kllb", description="克隆任务列表（含已完成）"),
            BotCommand(command="jtlb", description="监听任务列表"),
            BotCommand(command="klfail", description="查看克隆失败的条目"),
            BotCommand(command="retryfail", description="一键补发失败条目"),
            BotCommand(command="sd", description="查看/设置克隆速度阈值"),
            BotCommand(command="cfg", description="查看/覆盖配置项（存数据库）"),
            BotCommand(command="bf", description="查看/设置克隆并发数量"),
            BotCommand(command="speedtop", description="测速榜单前 40 名代理并自动选用最快"),
            BotCommand(command="speedall", description="测速全量代理榜并自动选用最快"),
            BotCommand(command="chapd", description="转发消息识别来源频道"),
            BotCommand(command="chaid", description="转发消息获取消息ID和链接"),
            BotCommand(command="help", description="全部命令与功能说明"),
            BotCommand(command="settings", description="限流与数据存储状态"),
            BotCommand(command="clone", description="克隆向导（旧）"),
            BotCommand(command="sync", description="克隆历史（旧命令）"),
            BotCommand(command="monitor", description="监听新消息（旧命令）"),
            BotCommand(command="list", description="任务列表（旧命令）"),
        ],
    ))
    logger.info("Bot 命令菜单已更新")

    # 注册 Bot 命令处理器
    primary_ub = userbot_manager.primary_userbot
    monitor_manager = MonitorManager(bot, primary_ub, db, config, userbot_manager=userbot_manager)
    runtime = register_handlers(bot, primary_ub, db, config, monitor_manager,
                               userbot_manager=userbot_manager)
    logger.info("命令处理器与按钮菜单已注册")

    # 恢复被上次进程中断的克隆任务（config.clone.auto_resume 控制）
    resumed, failed = await runtime.auto_resume_clones()
    if resumed or failed:
        logger.info("启动恢复：续传 %d 个克隆任务，%d 个续传失败已置为暂停", resumed, failed)
    await monitor_manager.restore_tasks()

    return monitor_manager


async def main():
    config = load_config()

    # 解析 MTProto 代理（off/auto/manual，默认 off 直连）
    proxy_candidate = None
    try:
        proxy = await _resolve_proxy(config)
    except ValueError as e:
        logger.critical("代理配置无效: %s", e)
        sys.exit(1)
    if proxy is not None:
        conn_cls, proxy_tuple = proxy
        proxy_candidate = state_to_candidate(load_state(
            normalize_config(config.get("proxy"))["state_file"]))
        if proxy_candidate is None:
            # manual 模式没有状态文件，从配置构造等价信息用于比较/复测
            m = normalize_config(config.get("proxy")).get("manual") or {}
            proxy_candidate = {
                "server": str(m["server"]), "port": int(m["port"]),
                "secret": str(m["secret"]), "latency_ms": None,
                "verified": True,
            }
        logger.info("已启用 MTProto 代理：%s:%d（连接类 %s）",
                    proxy_tuple[0], proxy_tuple[1], conn_cls.__name__)

    # 经代理时空载连接会被代理按空闲超时掐断（Telethon 默认 60 秒才发心跳），
    # 因此提前安装更短周期的心跳补丁；直连模式保持默认行为。
    _proxy_cfg = normalize_config(config.get("proxy"))
    if proxy is not None:
        install_keepalive(_proxy_cfg.get("keepalive_seconds",
                                         _proxy_cfg.get("keepalive", 20)))

    # 检查 cryptg 加速模块
    try:
        import cryptg
        logger.info("cryptg 加速模块已加载 (C/Rust AES)")
    except ImportError:
        logger.warning("未检测到 cryptg，当前使用低速 pure-python pyaes，大文件下载/上传速度可能受限")

    # 确保所需目录存在（数据库目录按 storage.db_path 的实际位置创建）
    db_path = resolve_db_path(config)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    os.makedirs("sessions", exist_ok=True)

    # 初始化数据库：先记录「升级前是否已有库」，再连接
    pre_audit = audit_db_path(db_path, config=config)
    db = Database(db_path, wal=wal_enabled(config))
    await db.init()
    storage_audit = audit_db_path(db_path, config=config)
    logger.info("数据库初始化完成 · %s", format_audit(storage_audit))

    # 应用数据库配置覆盖层（/cfg 在线调整过的项），必须在组件构造前合并
    overlay = await async_load_overlay(db)
    if overlay:
        apply_overlay(config, overlay)
        logger.info("已从数据库恢复 %d 项配置覆盖: %s",
                    len(overlay), ", ".join(sorted(overlay)))
    warning = persistence_warning(storage_audit, db_path)
    if warning:
        logger.warning("%s", warning)
    elif pre_audit["existed"]:
        logger.info("沿用已有数据库，历史任务与克隆进度保持不变")

    # 初始化 Bot 客户端（auto/manual 代理模式时走 MTProto 代理连接）
    bot = create_client(
        "sessions/bot", config["api_id"], config["api_hash"],
        proxy=proxy, connection_retries=10, retry_delay=5, timeout=30,
    )

    # 初始化多 UserBot 管理器
    userbot_manager = UserBotManager(config, proxy=proxy)

    # === 首次启动（连接失败会重新选代理并重试，不再直接崩溃循环） ===
    try:
        await _boot_start_clients(bot, userbot_manager, db, config,
                                  proxy_candidate)
    except UserBotNotAuthorized as e:
        # 这是配置/部署问题，不是程序缺陷：打印可执行的修复步骤后退出，
        # 完成登录后容器重启即可自动恢复。
        logger.critical("%s", e)
        logger.critical("进程退出（退出码 78），修复后 Docker 会自动重启并恢复运行")
        await db.close()
        sys.exit(78)
    await _setup_bot(bot, userbot_manager, db, config)

    # === 每小时代理复测（auto 模式）===
    if str(normalize_config(config.get("proxy")).get("mode") or "off").lower() == "auto":
        asyncio.create_task(_proxy_refresher(config, proxy_candidate))
        logger.info("代理自动复测任务已启动（每 %d 分钟）",
                    max(5, int(normalize_config(config.get("proxy")).get("refresh_minutes", 60))))

    # === 运行 + 断线重连循环 ===
    reconnect_count = 0
    try:
        while True:
            try:
                logger.info("所有服务就绪，等待消息...")
                await bot.run_until_disconnected()
            except (ConnectionError, OSError) as e:
                logger.error("连接丢失: %s", e)
            except Exception as e:
                logger.error("未预期异常: %s", e, exc_info=True)

            # run_until_disconnected() 返回 → 说明连接已断开，进入重连
            reconnect_count += 1
            if reconnect_count > MAX_RECONNECT_ATTEMPTS:
                logger.critical(
                    "连续 %d 次重连均失败，进程退出（交由 systemd 重启）",
                    MAX_RECONNECT_ATTEMPTS,
                )
                sys.exit(1)

            delay = min(
                RECONNECT_BASE_DELAY * (2 ** (reconnect_count - 1)),
                RECONNECT_MAX_DELAY,
            )
            logger.warning(
                "第 %d/%d 次重连，等待 %d 秒...",
                reconnect_count, MAX_RECONNECT_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)

            try:
                await bot.connect()
                await userbot_manager.connect_all()
                reconnect_count = 0   # 重连成功，重置计数
                logger.info("重连成功")
            except Exception as e:
                logger.error("重连失败: %s", e)
                # 回到 while 循环顶部，reconnect_count 已递增，继续退避
                # 自动代理模式下连续失败时触发重新选优（可能切换代理并重启）
                await _reconnect_proxy_check(reconnect_count, config, proxy_candidate)
    finally:
        # 正常退出/收到 KeyboardInterrupt 时把 WAL 合并回主库，确保宿主机的
        # bot.db 是完整一致的一份（进程被强杀时 WAL 也会在下次打开自动回放）。
        try:
            await db.close()
        except Exception as e:
            logger.warning("关闭数据库时出错: %s", e)


if __name__ == "__main__":
    asyncio.run(main())

