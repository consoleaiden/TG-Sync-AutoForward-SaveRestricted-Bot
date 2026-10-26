"""Bot 命令处理器：注册所有 Telegram Bot 命令。

交互有两条路径，共用同一套业务逻辑：
1. 按钮式菜单（bot/menu.py）：向导式创建任务，推荐路径；
2. 斜杠命令：保留给习惯命令行的用户与自动化脚本。
"""
import asyncio
import html
import json
import logging
import os
import re
import time

import yaml

from telethon import TelegramClient, events, errors, Button, utils
from telethon.tl import types
from telethon.tl.functions.channels import GetParticipantRequest, GetFullChannelRequest

from bot.link_parser import (
    ParsedLink,
    parse_link,
    resolve_chat_id,
    resolve_linked_chat,
)
from bot.menu import MenuController
from bot.handler_common import (
    STATUS_EMOJI,
    FetchTarget,
    ParsedSource,
    extract_tg_link,
)
from bot.telegram_utils import (
    resolve_topic_name, describe_source,
    get_target_topic_id, build_source_link, truncate,
)
from core.clone_plan import parse_start_time, progress_bar, to_local_time
from core.message_logic import (
    classify_message_kind,
    collect_album_messages,
    is_file_media,
)
from core.base_component import SyncComponentBase
from core.clone_gate import CloneGate
from core.syncer import Syncer
from core.restricted_syncer import RestrictedSyncer
from core.monitor import MonitorManager
from core.rate_limiter import _get_dynamic_rate_limit
from core.config_store import (
    apply_overlay, async_load_overlay, async_save_overlay, parse_value,
)
from core.speed_config import (
    OVERRIDE_KEY, apply_speed, merge_overrides, normalize_overrides,
    parse_speed_args,
)
from core.userbot_manager import UserBotManager
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.handlers")

# `/kl 100-1100 <链接>` 的 ID 区间（支持 - ~ — 到 至）
_RANGE_RE = re.compile(r"^(\d{1,12})\s*(?:-|~|—|到|至)\s*(\d{1,12})(?!\d)")
# 尾部区间：`/kljx <链接> 120-1000`（要求数字前有空白，避免吃掉链接里的数字）
_RANGE_RE_TAIL = re.compile(
    r"\s(\d{1,12})\s*(?:-|~|—|到|至)\s*(\d{1,12})(?!\d)\s*$")
# 单个起始 ID：`/kljx 120 <链接>`（前）或 `/kljx <链接> 120`（后）
_SINGLE_ID_HEAD = re.compile(r"^(\d{1,12})(?!\d)\s+")
_SINGLE_ID_TAIL = re.compile(r"\s(\d{1,12})(?!\d)\s*$")
# /chapd 的「等一条转发消息」有效期
CHAPD_TTL = 10 * 60


def parse_clone_args(raw: str) -> tuple[int, int, str, str | None]:
    """解析 `/kl 100-1100 <链接>`：返回 (起始ID, 结束ID, 剩余文本, 描述)。"""
    text = (raw or "").strip()
    start = end = 0
    desc = None
    m = _RANGE_RE.match(text)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            start, end = end, start
        desc = f"从消息 {start} 到 {end}"
        text = text[m.end():].strip()
    return start, end, text, desc


def parse_kljx_args(raw: str) -> tuple[int, int, str, str | None]:
    """解析 `/kljx <链接> [起始ID|起始ID-结束ID]`，ID 可写在链接前或后。

    返回 (起始ID, 结束ID, 剩余文本(链接), 描述)；没有 ID 时起止均为 0。
    """
    text = (raw or "").strip()
    if not text:
        return 0, 0, "", None

    # 1. 前置区间：`120-1000 <链接>`
    m = _RANGE_RE.match(text)
    if m:
        start, end, desc = _norm_range(m.group(1), m.group(2))
        return start, end, text[m.end():].strip(), desc

    # 2. 后置区间：`<链接> 120-1000`
    m = _RANGE_RE_TAIL.search(text)
    if m and m.start() > 0:
        start, end, desc = _norm_range(m.group(1), m.group(2))
        return start, end, text[:m.start()].strip(), desc

    # 3. 前置单个 ID：`120 <链接>`
    m = _SINGLE_ID_HEAD.match(text)
    if m and text[m.end():].strip():
        start = int(m.group(1))
        return start, 0, text[m.end():].strip(), f"从消息 {start} 开始"

    # 4. 后置单个 ID：`<链接> 120`
    m = _SINGLE_ID_TAIL.search(text)
    if m and m.start() > 0:
        start = int(m.group(1))
        return start, 0, text[:m.start()].strip(), f"从消息 {start} 开始"

    return 0, 0, text, None


def _norm_range(a: str, b: str) -> tuple[int, int, str]:
    start, end = int(a), int(b)
    if start > end:
        start, end = end, start
    return start, end, f"从消息 {start} 到 {end}"


def strip_command(raw: str, name: str) -> str:
    """去掉开头的 /name 或 /name@bot，保留参数部分。"""
    return re.sub(rf"^/{name}(?:@\w+)?\s*", "", raw or "", count=1).strip()


def describe_forwarded_source(msg) -> dict | None:
    """从转发消息里提取来源频道（名称 / 用户名 / ID / 链接）。"""
    hdr = getattr(msg, "fwd_from", None)
    fwd = getattr(msg, "forward", None)
    chat = getattr(fwd, "chat", None) if fwd else None

    chat_id = None
    if chat is not None:
        try:
            chat_id = utils.get_peer_id(chat)
        except Exception:
            chat_id = getattr(chat, "id", None)
    if chat_id is None and hdr is not None:
        peer = getattr(hdr, "from_id", None) or getattr(hdr, "saved_from_peer", None)
        if peer is not None:
            try:
                chat_id = utils.get_peer_id(peer)
            except Exception:
                chat_id = None
    if chat_id is None:
        return None

    title = (getattr(chat, "title", None)
             or getattr(hdr, "from_name", None) or "未知频道")
    username = getattr(chat, "username", None)
    if username:
        link = f"https://t.me/{username}"
    else:
        try:
            real_id = utils.resolve_id(int(chat_id))[0]
        except Exception:
            real_id = abs(int(chat_id))
        link = f"https://t.me/c/{real_id}/1"
    return {"chat_id": int(chat_id), "title": title,
            "username": username, "link": link}


def extract_forwarded_msg_id(msg) -> int:
    """从转发消息里取源频道中的**原始消息 ID**（不是转发副本的 ID）。

    频道消息转发时 `fwd_from.channel_post` 记录原 ID；个别客户端走
    `forward.channel_post`；从"已保存的消息"转发则只有 `saved_from_msg_id`。
    都没有（例如转发自个人）时返回 0。
    """
    hdr = getattr(msg, "fwd_from", None)
    fwd = getattr(msg, "forward", None)
    candidates = (
        getattr(hdr, "channel_post", None),
        getattr(fwd, "channel_post", None) if fwd is not None else None,
        getattr(hdr, "saved_from_msg_id", None),
    )
    for val in candidates:
        if val:
            return int(val)
    return 0


def build_message_link(chat_id: int, username: str | None, msg_id: int) -> str:
    """拼一条直达消息的 t.me 链接：公开频道用用户名，私有频道用 t.me/c/<真实ID>。"""
    if username:
        return f"https://t.me/{username}/{msg_id}"
    try:
        real_id = utils.resolve_id(int(chat_id))[0]
    except Exception:
        real_id = abs(int(chat_id))
    return f"https://t.me/c/{real_id}/{msg_id}"



async def latest_source_msg_id(source_id: int, topic_id=None, *clients) -> int | None:
    """取源（含话题）最新一条消息 ID；无法判定时返回 ``None``。

    ``None`` 表示"查不到"（客户端无权限 / 网络异常），调用方必须按"无法判断"处理，
    不能当成"没有新消息"，否则 `/kljx` 会误判成已是最新而漏补。

    多个客户端按顺序尝试（通常是 解析源时选中的 UserBot → 默认 UserBot → Bot），
    第一个能拿到结果的即为答案；源为空时返回 ``0``。
    """
    for c in clients:
        if not c:
            continue
        try:
            msgs = await c.get_messages(source_id, limit=1, reply_to=topic_id)
        except Exception as e:
            logger.debug("取源最新消息失败 source=%s: %s", source_id, e)
            continue
        if msgs is None:
            continue
        items = list(msgs)
        if not items:
            return 0
        mid = getattr(items[0], "id", None)
        return int(mid) if mid else 0
    return None


class HandlerRuntime:
    """register_handlers 的返回值：给 main.py 做启动时任务恢复。"""

    def __init__(self, auto_resume, sync_tasks: dict):
        self._auto_resume = auto_resume
        self.sync_tasks = sync_tasks

    async def auto_resume_clones(self) -> tuple[int, int]:
        return await self._auto_resume()


def register_handlers(bot: TelegramClient, userbot: TelegramClient,
                      db: Database, config: dict,
                      monitor_manager: MonitorManager,
                      userbot_manager: UserBotManager | None = None) -> HandlerRuntime:
    admin_ids = set(config.get("admin_ids", []))
    allow_public = config.get("allow_public_resolve", False)
    append_source_link = config.get("append_source_link", False)
    syncer = Syncer(bot, userbot, db, config, userbot_manager=userbot_manager)
    restricted_syncer = RestrictedSyncer(bot, userbot, db, config, userbot_manager=userbot_manager)
    forwarder = monitor_manager.forwarder
    _sync_tasks: dict[int, asyncio.Task] = {}
    # 克隆并发闸门：默认同时只跑 1 个克隆任务。
    # 每个 Syncer 自带一个 RateLimiter，同时跑 N 个时实际频率就是设计值 × N，
    # FloodWait 会成倍到来——用 /bf 调大前请想清楚风控代价。
    max_concurrent = max(1, int((config.get("clone") or {}).get("max_concurrent", 1)))
    _clone_gate = CloneGate(max_concurrent)
    # /sd 覆盖值要同时作用到所有 RateLimiter 实例（每个组件各持一个）
    _rate_limiters = [monitor_manager.rl, syncer.rl, restricted_syncer.rl]

    async def _load_speed_overrides() -> None:
        """启动时从数据库恢复 /sd 保存的速度覆盖值。"""
        try:
            raw = await models.get_setting(db, OVERRIDE_KEY)
            ov = normalize_overrides(json.loads(raw)) if raw else {}
        except Exception:
            ov = {}
        if ov:
            for rl in _rate_limiters:
                rl.set_overrides(ov)
            logger.info("已恢复速度覆盖值: %s", sorted(ov))

    try:
        asyncio.get_running_loop().create_task(_load_speed_overrides())
    except RuntimeError:
        pass  # 无事件循环（极端场景），跳过恢复，/sd 设置时仍会即时生效

    def is_admin(user_id: int) -> bool:
        return user_id in admin_ids

    def _is_admin_via_chat_identity(event) -> bool:
        """兼容匿名管理员/频道身份发言。"""
        if not (event.is_group or event.is_channel):
            return False
        msg = getattr(event, "message", None)
        if msg is None:
            return False
        from_id = getattr(msg, "from_id", None)
        if from_id is None:
            # 匿名管理员消息常见为 from_id 为空（Telethon）。
            return True
        return isinstance(from_id, types.PeerChannel)

    async def _chat_has_configured_admin(chat_id: int) -> bool:
        """检查目标群/频道中是否包含配置里的管理员。"""
        try:
            admins = await bot.get_participants(
                chat_id,
                filter=types.ChannelParticipantsAdmins(),
            )
        except Exception as e:
            logger.warning("拉取管理员列表失败 chat=%s err=%s", chat_id, e)
            return False
        admin_member_ids = {u.id for u in admins if getattr(u, "id", None) is not None}
        return bool(admin_member_ids & admin_ids)

    async def _require_admin(event, *, alert: bool = False) -> bool:
        """统一管理员校验。"""
        if event.sender_id is not None and is_admin(event.sender_id):
            return True
        if event.is_private:
            pass
        elif _is_admin_via_chat_identity(event):
            if await _chat_has_configured_admin(event.chat_id):
                return True
        if alert:
            await event.answer("⛔ 无权限", alert=True)
        else:
            await event.reply("⛔ 无权限")
        return False

    async def _get_task_or_alert(event, task_id: int) -> dict | None:
        """按 task_id 获取任务，不存在时给出回调提示。"""
        task = await models.get_task(db, task_id)
        if task:
            return task
        await event.answer("任务不存在", alert=True)
        return None

    # ------------------------------------------------------------------
    # 内部共享 helpers
    # ------------------------------------------------------------------

    async def _parse_source(event) -> ParsedSource | None:
        """从命令消息中解析链接并返回 ParsedSource。"""
        text = event.raw_text
        mode = "forward" if "--forward" in text else "copy"
        link = extract_tg_link(text)
        if not link:
            await event.reply("❌ 请提供有效的 Telegram 链接")
            return None
        parsed = parse_link(link)
        if not parsed:
            await event.reply("❌ 无法解析链接")
            return None
        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问源频道/群组")
            return None
        return ParsedSource(parsed=parsed, source_id=source_id, mode=mode)

    async def _resolve_topic_display_name(
        chat_id: int, topic_id: int | None, *, prefer_bot: bool = False
    ) -> str:
        """解析话题/线程名称，优先论坛话题名，失败回退线程顶层消息。"""
        if not topic_id:
            return ""

        fallback = f"#{topic_id}"
        forum_clients = [userbot] if prefer_bot else [userbot, bot]
        for client in forum_clients:
            name = await resolve_topic_name(client, chat_id, topic_id)
            if name and name != fallback:
                return name

        # General 话题 (id=1) 没有对应的 messageActionTopicCreate 服务消息，
        # 不能用 get_messages(ids=1) 回退，否则会取到第一条用户消息的文本。
        if topic_id == 1:
            return "General"

        # 非 forum 场景（评论区线程/普通线程）也可能带 top_msg_id。
        thread_clients = [bot, userbot] if prefer_bot else [userbot, bot]
        for client in thread_clients:
            try:
                top_msg = await client.get_messages(chat_id, ids=topic_id)
            except Exception:
                continue
            if not top_msg:
                continue
            action = getattr(top_msg, "action", None)
            action_title = getattr(action, "title", None) if action else None
            if action_title:
                return action_title
            raw = (getattr(top_msg, "raw_text", None) or "").strip()
            if raw:
                return truncate(raw.splitlines()[0], 20)
            text = (getattr(top_msg, "message", None) or "").strip()
            if text:
                return truncate(text.splitlines()[0], 20)

        return "未知线程"

    async def _describe_source_for_display(
        source_id: int, parsed: ParsedLink, client: TelegramClient | None = None
    ) -> str:
        """构造源描述，确保话题展示优先使用真实名称。"""
        ub = client or userbot
        source_desc = await describe_source(ub, source_id, parsed)
        if not parsed.topic_id:
            return source_desc
        topic_name = await _resolve_topic_display_name(source_id, parsed.topic_id)
        return source_desc.replace(
            f"话题「#{parsed.topic_id}」",
            f"话题「{topic_name}」",
        )

    async def _resolve_chat_meta(
        client: TelegramClient, chat_id: int
    ) -> tuple[str, str, str]:
        """解析实体元信息: (kind, visibility, name)。"""
        try:
            entity = await client.get_entity(chat_id)
            if isinstance(entity, types.Channel):
                kind = "频道" if getattr(entity, "broadcast", False) else "群组"
                username = getattr(entity, "username", None)
                if username:
                    return kind, "公开", f"@{username}"
                title = getattr(entity, "title", None) or str(chat_id)
                return kind, "私有", title

            title = getattr(entity, "title", None)
            if title:
                return "群组", "", title
            first_name = getattr(entity, "first_name", None)
            if first_name:
                return "群组", "", first_name
        except Exception:
            pass
        return "频道/群组", "", str(chat_id)

    async def _format_chat_topic_display(
        chat_id: int,
        topic_id: int | None,
        *,
        prefer_bot: bool = False,
        short: bool = False,
    ) -> str:
        """统一格式化: 频道/群组 + 话题。"""
        clients = [bot, userbot] if prefer_bot else [userbot, bot]
        kind, visibility, name = "频道/群组", "", str(chat_id)
        for client in clients:
            kind, visibility, name = await _resolve_chat_meta(client, chat_id)
            if name != str(chat_id):
                break

        if short:
            base = f"{kind[:1]}:{truncate(name, 12)}"
        else:
            vis_part = f"{visibility} " if visibility else ""
            base = f"{kind} | {vis_part}{name}"

        if topic_id:
            topic_name = await _resolve_topic_display_name(
                chat_id, topic_id, prefer_bot=prefer_bot
            )
            if short:
                base += f"/{truncate(topic_name, 8)}"
            else:
                base += f" + 话题「{topic_name}」"
        return base

    async def _ensure_userbot_joined_source_for_monitor(
        source_chat_id: int, source_topic_id: int | None
    ) -> tuple[bool, str | None, TelegramClient | None]:
        """校验 monitor 前提: userbot 已加入源。返回 (ok, reason, active_userbot)。"""
        active_ub = None
        if userbot_manager:
            active_ub, _ = await userbot_manager.resolve_accessible_userbot(source_chat_id)
        if not active_ub:
            active_ub = userbot

        try:
            entity = await active_ub.get_entity(source_chat_id)
        except (ValueError, errors.ChannelPrivateError):
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 要求 UserBot 必须先加入源后才会生效。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该源，再执行 /monitor。",
                None,
            )
        except Exception as e:
            logger.warning("校验 userbot 源加入状态失败 source=%s err=%s", source_chat_id, e)
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ 无法确认 UserBot 对源的可访问性，请先确保 UserBot 已加入源。\n"
                f"📌 源: {source_text}",
                None,
            )

        # Channel/超级群走参与者校验；其它会话默认放行。
        if not isinstance(entity, types.Channel):
            return True, None, active_ub

        try:
            me = await active_ub.get_me()
            await active_ub(GetParticipantRequest(channel=entity, participant=me.id))
            return True, None, active_ub
        except errors.UserNotParticipantError:
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 未生效：UserBot 尚未加入源。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该频道/群组后，再执行 /monitor。",
                None,
            )
        except errors.ChannelPrivateError:
            source_text = await _format_chat_topic_display(
                source_chat_id, source_topic_id
            )
            return (
                False,
                "❌ /monitor 未生效：源为私有且 UserBot 无访问权限。\n"
                f"📌 源: {source_text}\n"
                "请先让 UserBot 加入该源后重试。",
                None,
            )
        except Exception as e:
            logger.warning("GetParticipant 校验失败 source=%s err=%s", source_chat_id, e)
            # 某些场景参与者列表受限，回退到读取探测。
            try:
                await active_ub.get_messages(source_chat_id, limit=1)
                return True, None, active_ub
            except Exception:
                source_text = await _format_chat_topic_display(
                    source_chat_id, source_topic_id
                )
                return (
                    False,
                    "❌ /monitor 要求 UserBot 可读取源消息，但当前不可读。\n"
                    f"📌 源: {source_text}\n"
                    "请先让 UserBot 加入该源后重试。",
                    None,
                )

    async def _stop_running_task(task: dict):
        """停止一个运行中的任务（sync / sync_restricted / monitor）。"""
        tid = task["id"]
        if task["type"] in ("sync", "sync_restricted"):
            syncer.cancel(tid)
            restricted_syncer.cancel(tid)
            at = _sync_tasks.pop(tid, None)
            if at:
                at.cancel()
        else:
            await monitor_manager.stop_monitor(tid)

    async def _build_task_buttons(tasks: list) -> list:
        """为任务列表构建单列 inline button。"""
        buttons = []
        for t in tasks:
            emoji = "🔄" if t["type"] in ("sync", "sync_restricted") else "👁"
            status = STATUS_EMOJI.get(t["status"], "❓")
            follow = "🛰" if int(t.get("follow") or 0) else ""
            src = await _format_chat_topic_display(
                t["source_chat_id"], t["source_topic_id"], short=True
            )
            label = f"{status}{emoji}{follow} #{t['id']} {src}"
            buttons.append([Button.inline(truncate(label, 48), data=f"task:{t['id']}")])
        buttons.append([Button.inline("🗑 清空所有任务", data="clear_all")])
        return buttons

    # ------------------------------------------------------------------
    # 克隆任务：创建 / 续传 / 停止（菜单与命令共用）
    # ------------------------------------------------------------------

    def _track(task_id: int, task: asyncio.Task) -> None:
        _sync_tasks[task_id] = task
        task.add_done_callback(lambda _t, tid=task_id: _sync_tasks.pop(tid, None))

    def _spawn_sync(task_id: int, *, source_id: int, target_chat_id: int, mode: str,
                    source_topic_id: int | None, target_topic_id: int | None,
                    notify_chat_id: int | None, notify_msg_id: int | None,
                    userbot: TelegramClient | None) -> None:
        async def _runner() -> None:
            # 排队提示只在前方确实有任务时发一次，避免续传时刷屏
            if _clone_gate.full:
                logger.info("克隆并发已达上限 %d，任务 #%d 排队等待",
                            _clone_gate.limit, task_id)
                try:
                    await bot.send_message(
                        notify_chat_id or target_chat_id,
                        f"⏳ 任务 #{task_id} 已排队：当前已有 {_clone_gate.limit} 个克隆在跑，"
                        f"前面的跑完会自动开始（/bf 可调并发，但同时跑多个限流会成倍到来）",
                        reply_to=target_topic_id or None)
                except Exception as e:
                    logger.debug("发送排队提示失败: %s", e)
            async with _clone_gate:
                await syncer.start_sync(
                    task_id, source_id, target_chat_id, mode,
                    source_topic_id=source_topic_id,
                    target_topic_id=target_topic_id,
                    notify_chat_id=notify_chat_id,
                    notify_topic_id=target_topic_id,
                    notify_reply_to_msg_id=notify_msg_id,
                    userbot=userbot)

        task = asyncio.create_task(_runner())
        _track(task_id, task)

    async def _spawn_restricted(task_id: int, t: dict,
                                userbot: TelegramClient | None = None) -> None:
        async def _runner() -> None:
            async with _clone_gate:
                await restricted_syncer.start_sync(
                    task_id, t["source_chat_id"], t["target_chat_id"],
                    source_topic_id=t["source_topic_id"],
                    target_topic_id=t["target_topic_id"],
                    notify_chat_id=t.get("notify_chat_id") or t["target_chat_id"],
                    notify_topic_id=t["target_topic_id"],
                    notify_reply_to_msg_id=t.get("notify_msg_id"),
                    userbot=userbot)

        task = asyncio.create_task(_runner())
        _track(task_id, task)

    async def _create_clone_task(*, source_id: int, target_chat_id: int, mode: str,
                                 source_topic_id: int | None = None,
                                 target_topic_id: int | None = None,
                                 start_msg_id: int = 0,
                                 start_date: str | None = None,
                                 end_msg_id: int = 0,
                                 follow: int = 0,
                                 source_title: str | None = None,
                                 target_title: str | None = None,
                                 notify_chat_id: int | None = None,
                                 created_by: int | None = None,
                                 userbot: TelegramClient | None = None,
                                 start_desc: str = "从头开始（最早 → 最新）") -> int:
        """建任务行 → 发启动消息（作为进度载体）→ 后台执行。"""
        task_id = await models.create_task(
            db, "sync", source_id, target_chat_id, mode,
            source_topic_id=source_topic_id,
            target_topic_id=target_topic_id,
            start_msg_id=start_msg_id,
            start_date=start_date,
            end_msg_id=end_msg_id,
            follow=follow,
            source_title=source_title,
            target_title=target_title,
            notify_chat_id=notify_chat_id,
            created_by=created_by)

        lines = [
            f"🚀 克隆任务 #{task_id} 已启动",
            f"📌 源: {source_title or source_id}",
            f"🎯 目标: {target_title or target_chat_id}",
            f"📍 起点: {start_desc}",
            f"📋 模式: {mode}",
        ]
        if follow:
            lines.append("🛰 完成后会自动开启「持续监听」，后续新消息继续克隆")
        lines.append("⏳ 正在统计待克隆消息数量…")
        notify_msg_id = None
        try:
            msg = await bot.send_message(
                notify_chat_id or target_chat_id, "\n".join(lines),
                reply_to=target_topic_id or None,
                buttons=[[Button.inline("📋 详情", data=f"m:task:{task_id}"),
                          Button.inline("⏸ 暂停", data=f"m:pause:{task_id}")]])
            notify_msg_id = msg.id
            await models.update_task_notify(
                db, task_id, notify_chat_id or target_chat_id, msg.id)
        except Exception as e:
            logger.warning("发送任务启动消息失败（进度仅写日志）: %s", e)

        _spawn_sync(
            task_id, source_id=source_id, target_chat_id=target_chat_id, mode=mode,
            source_topic_id=source_topic_id, target_topic_id=target_topic_id,
            notify_chat_id=notify_chat_id or target_chat_id,
            notify_msg_id=notify_msg_id, userbot=userbot)
        return task_id

    async def _resume_clone(task_id: int, *, allow_completed: bool = False
                            ) -> tuple[bool, str | None]:
        """从游标继续一个未完成的克隆/监控任务。"""
        t = await models.get_task(db, task_id)
        if not t:
            return False, "任务不存在"

        if t["type"] == "monitor":
            ok, reason, ub = await _ensure_userbot_joined_source_for_monitor(
                t["source_chat_id"], t["source_topic_id"])
            if not ok:
                return False, truncate((reason or "UserBot 未加入源").replace("\n", " "), 120)
            await models.update_task_status(db, task_id, "running")
            await monitor_manager.start_monitor(
                task_id, t["source_chat_id"], t["target_chat_id"], t["mode"],
                t["source_topic_id"], t["target_topic_id"], userbot=ub)
            logger.info("监控任务 #%s 已恢复", task_id)
            return True, None

        if (int(t.get("last_synced_msg_id") or 0) <= 0
                and t["status"] == "completed" and not allow_completed):
            return False, "该任务已完成，无需继续"

        await models.update_task_status(db, task_id, "running")
        if t["type"] == "sync_restricted":
            await _spawn_restricted(task_id, t)
        else:
            _spawn_sync(
                task_id,
                source_id=t["source_chat_id"],
                target_chat_id=t["target_chat_id"],
                mode=t["mode"],
                source_topic_id=t["source_topic_id"],
                target_topic_id=t["target_topic_id"],
                notify_chat_id=t.get("notify_chat_id") or t["target_chat_id"],
                notify_msg_id=t.get("notify_msg_id"),
                userbot=None)
        logger.info("克隆任务 #%s 已从游标 %s 继续", task_id, t.get("last_synced_msg_id"))
        return True, None

    async def _stop_task(task_id: int) -> None:
        t = await models.get_task(db, task_id)
        if not t:
            return
        if t["status"] == "running":
            await _stop_running_task(t)

    # ------------------------------------------------------------------
    # 持续监听（克隆完成后默认开启，可在任务详情里开关）
    # ------------------------------------------------------------------

    async def _notify_simple(task: dict, text: str) -> None:
        chat_id = task.get("notify_chat_id") or task.get("target_chat_id")
        if not chat_id:
            return
        try:
            await bot.send_message(
                chat_id, text, reply_to=task.get("target_topic_id") or None)
        except Exception as e:
            logger.warning("发送任务通知失败 task=%s: %s", task.get("id"), e)

    async def _notify_follow(clone_task: dict, monitor_id: int, *, reason: str) -> None:
        src = clone_task.get("source_title") or clone_task["source_chat_id"]
        tgt = clone_task.get("target_title") or clone_task["target_chat_id"]
        text = (f"🛰 {reason}\n"
                f"克隆任务 #{clone_task['id']} 会继续监听后续新消息"
                f"（监听任务 #{monitor_id}）\n"
                f"📌 源: {src}\n🎯 目标: {tgt}\n\n"
                "不需要继续跟新时可点下面的按钮关闭，克隆记录会保留。")
        chat_id = clone_task.get("notify_chat_id") or clone_task["target_chat_id"]
        try:
            await bot.send_message(
                chat_id, text, reply_to=clone_task.get("target_topic_id") or None,
                buttons=[[Button.inline("🛰 关闭持续监听",
                                        data=f"m:follow:{clone_task['id']}"),
                          Button.inline("📋 任务列表", data="m:tasks")]])
        except Exception as e:
            logger.warning("发送持续监听提示失败 clone=%s: %s", clone_task.get("id"), e)

    async def _ensure_follow_monitor(clone_task_id: int, *, notify: bool = True,
                                     reason: str = "已开启持续监听"
                                     ) -> tuple[int | None, str | None]:
        """确保克隆任务对应的监听任务在运行；返回 (monitor_task_id, err)。"""
        t = await models.get_task(db, clone_task_id)
        if not t:
            return None, "任务不存在"
        if t["type"] not in models.CLONE_TASK_TYPES:
            return None, "只有克隆任务可以开启持续监听"

        existing = await models.find_follow_task(db, t)
        if existing:
            if existing["status"] != "running":
                ok, why, ub = await _ensure_userbot_joined_source_for_monitor(
                    existing["source_chat_id"], existing["source_topic_id"])
                if not ok:
                    return existing["id"], truncate(
                        (why or "UserBot 未加入源").replace("\n", " "), 150)
                await models.update_task_status(db, existing["id"], "running")
                await monitor_manager.start_monitor(
                    existing["id"], existing["source_chat_id"],
                    existing["target_chat_id"], existing["mode"],
                    existing["source_topic_id"], existing["target_topic_id"],
                    userbot=ub)
                if notify:
                    await _notify_follow(t, existing["id"], reason="已恢复持续监听")
            await models.set_follow(db, clone_task_id, 1, existing["id"])
            return existing["id"], None

        ok, why, ub = await _ensure_userbot_joined_source_for_monitor(
            t["source_chat_id"], t.get("source_topic_id"))
        if not ok:
            return None, truncate((why or "UserBot 未加入源").replace("\n", " "), 150)

        mid, err, _created = await _create_monitor_task(
            source_id=t["source_chat_id"],
            target_chat_id=t["target_chat_id"],
            mode=t["mode"],
            source_topic_id=t.get("source_topic_id"),
            target_topic_id=t.get("target_topic_id"),
            source_title=t.get("source_title"),
            target_title=t.get("target_title"),
            notify_chat_id=t.get("notify_chat_id") or t["target_chat_id"],
            created_by=t.get("created_by"),
            parent_id=clone_task_id,
            userbot=ub)
        if not mid:
            return None, err or "创建监听任务失败"
        await models.set_follow(db, clone_task_id, 1, mid)
        if notify:
            await _notify_follow(t, mid, reason=reason)
        return mid, None

    async def _toggle_follow(clone_task_id: int) -> tuple[bool, str]:
        """任务详情按钮：开启 / 关闭某个克隆任务的持续监听。"""
        t = await models.get_task(db, clone_task_id)
        if not t:
            return False, "任务不存在"
        if t["type"] not in models.CLONE_TASK_TYPES:
            return False, "只有克隆任务支持持续监听"

        follow_on = bool(int(t.get("follow") or 0))
        monitor = await models.find_follow_task(db, t)
        if follow_on and monitor and monitor.get("status") == "running":
            await _stop_running_task(monitor)
            await models.set_follow(db, clone_task_id, 0)
            return True, f"🛰 已关闭持续监听（监听任务 #{monitor['id']} 已暂停，记录保留）"

        mid, err = await _ensure_follow_monitor(clone_task_id, notify=False)
        if not mid:
            return False, err or "开启持续监听失败"
        return True, f"🛰 已开启持续监听（监听任务 #{mid}）"

    async def _continue_clone(task_id: int) -> tuple[bool, str | None]:
        """「继续补新消息」：已完成的任务重开（保留游标），未完成的直接续传。"""
        t = await models.get_task(db, task_id)
        if not t:
            return False, "任务不存在"
        if t["status"] == "completed":
            await models.reopen_clone_task(
                db, task_id, follow=int(t.get("follow") or 0), clear_end=True)
        ok, err = await _resume_clone(task_id, allow_completed=True)
        if not ok:
            await models.update_task_status(db, task_id, "paused")
        return ok, err

    async def _on_clone_completed(clone_task_id: int) -> None:
        """克隆跑完后的默认动作：转成持续监听。"""
        cfg = config.get("clone") or {}
        if not cfg.get("follow_after_finish", True):
            return
        t = await models.get_task(db, clone_task_id) or {}
        if not int(t.get("follow") or 0):
            return
        mid, err = await _ensure_follow_monitor(clone_task_id, notify=False)
        if mid:
            await _notify_follow(t, mid, reason="✅ 克隆完成，已自动转为持续监听")
        elif err:
            logger.warning("克隆任务 #%s 完成后开启持续监听失败: %s", clone_task_id, err)
            await _notify_simple(
                t, f"⚠️ 克隆已完成，但持续监听未开启：{err}\n"
                   f"可在「📋 任务列表」里点任务 #{clone_task_id} 手动重试。")

    # ------------------------------------------------------------------
    # 菜单回调：把网络/业务细节收敛在这里，菜单只负责交互
    # ------------------------------------------------------------------

    async def _menu_is_admin(event) -> bool:
        alert = getattr(event, "data", None) is not None
        return await _require_admin(event, alert=alert)

    async def _menu_resolve_source(event, raw: str):
        text = (raw or "").strip()
        if not text:
            return None, "请发送源频道链接"
        if text.startswith("@"):
            text = f"https://t.me/{text[1:]}"

        parsed = None
        if re.fullmatch(r"-?\d{5,}", text):
            source_id = int(text)
        else:
            link = extract_tg_link(text) or text
            parsed = parse_link(link)
            if not parsed:
                return None, "无法解析该链接，请检查格式"
            source_id = await resolve_chat_id(userbot, parsed)
            if not source_id:
                return None, "无法访问该频道/群组（UserBot 可能未加入）"

        topic_id = parsed.topic_id if parsed else None
        is_private = parsed.is_private if parsed else str(source_id).startswith("-100")

        active_ub = userbot
        if userbot_manager:
            active_ub, err = await userbot_manager.resolve_accessible_userbot(source_id)
            if not active_ub:
                if is_private:
                    return None, err or "所有 UserBot 均未加入该私有频道/群组"
                active_ub = userbot

        title = await _format_chat_topic_display(source_id, topic_id)
        total = None
        try:
            msgs = await active_ub.get_messages(source_id, limit=1, reply_to=topic_id)
            total = getattr(msgs, "total", None)
        except Exception as e:
            logger.debug("统计源消息数失败 source=%s: %s", source_id, e)
        logger.info("菜单解析源: %s (topic=%s, total=%s)", title, topic_id, total)
        return {
            "chat_id": source_id, "topic_id": topic_id, "title": title,
            "total": total, "userbot": active_ub,
        }, None

    async def _menu_resolve_target(event, raw: str):
        text = (raw or "").strip()
        if not text:
            return None, "请发送目标频道链接"
        if text.startswith("@"):
            text = f"https://t.me/{text[1:]}"

        topic_id = None
        if re.fullmatch(r"-?\d{5,}", text):
            chat_id = int(text)
        else:
            link = extract_tg_link(text) or text
            parsed = parse_link(link)
            if not parsed:
                return None, "无法解析目标链接"
            chat_id = await resolve_chat_id(bot, parsed)
            if not chat_id:
                chat_id = await resolve_chat_id(userbot, parsed)
            if not chat_id:
                return None, "无法解析目标频道（Bot 可能不在该频道中）"
            topic_id = parsed.topic_id

        try:
            await bot.get_entity(chat_id)
        except Exception as e:
            return None, f"Bot 无法访问该目标频道: {e}"

        title = await _format_chat_topic_display(chat_id, topic_id, prefer_bot=True)
        return {"chat_id": chat_id, "topic_id": topic_id, "title": title}, None

    async def _create_monitor_task(*, source_id: int, target_chat_id: int,
                                   mode: str,
                                   source_topic_id: int | None = None,
                                   target_topic_id: int | None = None,
                                   source_title: str | None = None,
                                   target_title: str | None = None,
                                   notify_chat_id: int | None = None,
                                   created_by: int | None = None,
                                   parent_id: int | None = None,
                                   userbot: TelegramClient | None = None,
                                   ) -> tuple[int | None, str | None, bool]:
        """建监控任务 → 去重 → 发确认消息 → 注册监听。

        返回 (task_id, err, created)。同一「源话题→目标话题」已有运行中/暂停的
        监控任务时直接复用，不重复创建（created=False），避免同一消息被转发两次。
        """
        existing = await models.find_monitor_task(
            db, source_id, target_chat_id, source_topic_id, target_topic_id)
        if existing:
            logger.info("监控任务重复：已有 #%s（%s），跳过创建",
                        existing["id"], existing["status"])
            return existing["id"], "exists", False

        task_id = await models.create_task(
            db, "monitor", source_id, target_chat_id, mode,
            source_topic_id=source_topic_id,
            target_topic_id=target_topic_id,
            source_title=source_title,
            target_title=target_title,
            notify_chat_id=notify_chat_id,
            created_by=created_by,
            parent_id=parent_id)
        await monitor_manager.start_monitor(
            task_id, source_id, target_chat_id, mode,
            source_topic_id=source_topic_id,
            target_topic_id=target_topic_id,
            userbot=userbot)

        src_desc = source_title or await _format_chat_topic_display(
            source_id, source_topic_id)
        tgt_desc = target_title or await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True)
        scope = f"仅话题 #{source_topic_id}" if source_topic_id else "全部新消息"
        lines = [
            f"👁 监控任务 #{task_id} 已创建，正在监听",
            f"📌 源: {src_desc}",
            f"🎯 目标: {tgt_desc}",
            f"📋 模式: {'复制' if mode == 'copy' else '转发'}",
            f"🧩 范围: {scope}",
            "",
            "收到新消息会自动转发到目标；已转发条数与最近活动可在「监控任务」里查看。",
        ]
        chat_id = notify_chat_id or target_chat_id
        try:
            msg = await bot.send_message(
                chat_id, "\n".join(lines),
                reply_to=target_topic_id or None,
                buttons=[[Button.inline("📋 详情", data=f"m:task:{task_id}"),
                          Button.inline("👁 监控任务", data="m:monitors")],
                         [Button.inline("⏸ 暂停", data=f"m:pause:{task_id}")]])
            await models.update_task_notify(db, task_id, chat_id, msg.id)
        except Exception as e:
            logger.warning("发送监控任务确认消息失败: %s", e)

        logger.info("监控任务 #%s 创建成功: source=%s topic=%s -> target=%s",
                    task_id, source_id, source_topic_id, target_chat_id)
        return task_id, None, True

    async def _menu_create_task(payload: dict, event):
        kind = payload.get("kind")
        source_id = payload["source_chat_id"]
        source_topic_id = payload.get("source_topic_id")
        target_chat_id = payload["target_chat_id"]
        target_topic_id = payload.get("target_topic_id")
        mode = payload.get("mode") or "copy"
        start = payload.get("start") or {}

        if kind == "monitor":
            # 先去重再校验 UserBot：重复时不必再发一轮 RPC 探测
            existing = await models.find_monitor_task(
                db, source_id, target_chat_id, source_topic_id, target_topic_id)
            if existing:
                return existing["id"], "exists"
            ok, reason, ub = await _ensure_userbot_joined_source_for_monitor(
                source_id, source_topic_id)
            if not ok:
                return None, truncate((reason or "UserBot 未加入源").replace("\n", " "), 150)
            task_id, err, created = await _create_monitor_task(
                source_id=source_id,
                target_chat_id=target_chat_id,
                mode=mode,
                source_topic_id=source_topic_id,
                target_topic_id=target_topic_id,
                source_title=payload.get("source_title"),
                target_title=payload.get("target_title"),
                notify_chat_id=payload.get("chat_id"),
                created_by=payload.get("sender_id"),
                userbot=ub or payload.get("userbot"))
            if err and err != "exists":
                return None, err
            return task_id, (None if created else "exists")

        # 「继续上次进度」= 复用原任务，不新建
        if start.get("kind") == "resume" and start.get("task_id"):
            prev_id = int(start["task_id"])
            ok, err = await _resume_clone(prev_id)
            return (prev_id, None) if ok else (None, err)

        task_id = await _create_clone_task(
            source_id=source_id,
            target_chat_id=target_chat_id,
            mode=mode,
            source_topic_id=source_topic_id,
            target_topic_id=target_topic_id,
            start_msg_id=int(start.get("msg_id") or 0),
            start_date=start.get("date"),
            end_msg_id=int(start.get("end_msg_id") or 0),
            follow=1,          # 菜单建的克隆任务默认持续监听后续新消息
            source_title=payload.get("source_title"),
            target_title=payload.get("target_title"),
            notify_chat_id=payload.get("chat_id"),
            created_by=payload.get("sender_id"),
            userbot=payload.get("userbot"),
            start_desc=start.get("desc") or "从头开始（最早 → 最新）",
        )
        return task_id, None

    async def _auto_resume_clones() -> tuple[int, int]:
        """进程重启后恢复被中断的克隆任务（可在 config.clone.auto_resume 关闭）。"""
        cfg = config.get("clone") or {}
        interrupted = await models.get_interrupted_clones(db)
        if not interrupted:
            return 0, 0
        if not cfg.get("auto_resume", True):
            for t in interrupted:
                await models.update_task_status(db, t["id"], "paused")
            logger.info("已暂停 %d 个被中断的克隆任务（auto_resume 已关闭）", len(interrupted))
            return 0, 0

        resumed = failed = 0
        for t in interrupted:
            ok, err = await _resume_clone(t["id"])
            if ok:
                resumed += 1
            else:
                failed += 1
                await models.update_task_status(db, t["id"], "paused")
                logger.warning("克隆任务 #%s 自动续传失败（已置为暂停）: %s", t["id"], err)
        if resumed:
            logger.info("已自动续传 %d 个克隆任务", resumed)
        return resumed, failed

    # 克隆完成 → 默认转「持续监听」（config.clone.follow_after_finish 可关）
    syncer.on_complete = _on_clone_completed

    menu = MenuController(
        bot,
        db=db,
        config=config,
        userbot=userbot,
        is_admin=_menu_is_admin,
        format_display=lambda chat_id, topic_id, prefer_bot=False:
            _format_chat_topic_display(chat_id, topic_id, prefer_bot=prefer_bot),
        resolve_source=_menu_resolve_source,
        resolve_target=_menu_resolve_target,
        create_task=_menu_create_task,
        stop_task=_stop_task,
        resume_task=_resume_clone,
        toggle_follow=_toggle_follow,
        continue_task=_continue_clone,
        monitor_manager=monitor_manager,
    )
    menu.register()
    logger.info("按钮菜单已注册")

    async def _resolve_private_fetch_target(parsed: ParsedLink,
                                            source_id: int,
                                            client: TelegramClient | None = None) -> FetchTarget | None:
        """将私聊解析的 parsed link 解析为真实抓取 chat/msg。"""
        fetch_chat_id = source_id
        target_msg_id = parsed.msg_id
        ub = client or userbot
        if parsed.comment_id:
            linked_id = await resolve_linked_chat(ub, source_id)
            if not linked_id:
                return None
            fetch_chat_id = linked_id
            target_msg_id = parsed.comment_id
            logger.info("评论链接 → 讨论群 %s 消息 %s", fetch_chat_id, parsed.comment_id)
        return FetchTarget(chat_id=fetch_chat_id, msg_id=target_msg_id)

    async def _pre_warm_bot_cache(
        parsed: ParsedLink, source_id: int, fetch_chat_id: int
    ):
        """尝试预热 Bot 实体缓存，使策略1(Bot直接)能命中。

        在 handler 层仍持有原始用户名字符串 (parsed.chat_id)，
        利用它让 Bot 主动向 Telegram 解析并缓存 access_hash。
        """
        # 已有缓存则跳过
        try:
            await bot.get_input_entity(fetch_chat_id)
            return
        except Exception:
            pass

        original_username = (
            parsed.chat_id if isinstance(parsed.chat_id, str) else None
        )
        if not original_username:
            return  # 私有链接(纯数字 ID)，无用户名可用

        try:
            channel_entity = await bot.get_entity(original_username)
            logger.info("Bot 预热: 已解析 @%s", original_username)
        except Exception as e:
            logger.debug("Bot 预热 @%s 失败: %s", original_username, e)
            return

        try:
            await bot.get_input_entity(channel_entity)
            logger.info("Bot 预热: 已缓存源 peer @%s", original_username)
        except Exception as e:
            logger.debug("Bot 预热源 peer 失败 @%s: %s", original_username, e)

        # 评论链接: fetch_chat_id 是讨论群而非频道，需额外解析讨论群
        if parsed.comment_id and fetch_chat_id != source_id:
            try:
                await bot(GetFullChannelRequest(channel_entity))
                logger.info("Bot 预热: 已解析讨论群(来自 @%s)", original_username)
            except Exception as e:
                logger.debug("Bot 预热讨论群失败: %s", e)
            try:
                discussion_entity = await bot.get_entity(fetch_chat_id)
                await bot.get_input_entity(discussion_entity)
                logger.info("Bot 预热: 已缓存讨论群 peer %s", fetch_chat_id)
            except Exception as e:
                logger.debug("Bot 预热讨论群 peer 失败 %s: %s", fetch_chat_id, e)

    def _build_source_url(raw_text: str, parsed: ParsedLink, source_id: int) -> str:
        raw_link = extract_tg_link(raw_text)
        if raw_link:
            cleaned = re.sub(r'([?&])single\b(=[^&]*)?', '', raw_link)
            cleaned = cleaned.replace("?&", "?").rstrip("?&")
            if cleaned.startswith("http://") or cleaned.startswith("https://"):
                return cleaned
        if parsed.is_private:
            clean_id = str(source_id).replace("-100", "").lstrip("-")
            if parsed.topic_id:
                url = f"https://t.me/c/{clean_id}/{parsed.topic_id}/{parsed.msg_id}"
            else:
                url = f"https://t.me/c/{clean_id}/{parsed.msg_id}"
        else:
            if parsed.topic_id:
                url = f"https://t.me/{parsed.chat_id}/{parsed.topic_id}/{parsed.msg_id}"
            else:
                url = f"https://t.me/{parsed.chat_id}/{parsed.msg_id}"
        if parsed.comment_id:
            url += f"?comment={parsed.comment_id}"
        return url

    async def _append_link_to_target(target_chat_id: int, target_msg_id: int,
                                     base_text: str, source_url: str,
                                     is_media: bool = False):
        link_tag = f"[Source Link]({source_url})"
        max_len = 1024 if is_media else 4096
        base = base_text or ""
        if base:
            combined = f"{base}\n\n{link_tag}"
            if len(combined) > max_len:
                overflow = len(combined) - max_len
                cut_len = max(0, len(base) - overflow - 3)
                base = base[:cut_len] + "..."
                combined = f"{base}\n\n{link_tag}"
        else:
            combined = link_tag

        logger.info("私聊来源链接准备追加: target_msg=%s is_media=%s source_url=%s",
                    target_msg_id, is_media, source_url)
        try:
            await bot.edit_message(
                target_chat_id, target_msg_id,
                text=combined,
                parse_mode="md",
                link_preview=False,
            )
            logger.info("私聊来源链接追加成功: target_msg=%s", target_msg_id)
        except errors.MessageNotModifiedError:
            pass
        except Exception as e:
            logger.warning("私聊编辑追加 Markdown 来源链接失败 (%s: %s)，尝试 HTML 回退",
                           type(e).__name__, e)
            try:
                html_link = f'<a href="{html.escape(source_url)}">Source Link</a>'
                if base:
                    esc_base = html.escape(base)
                    html_combined = f"{esc_base}\n\n{html_link}"
                    if len(html_combined) > max_len:
                        overflow = len(html_combined) - max_len
                        cut_len = max(0, len(esc_base) - overflow - 3)
                        esc_base = esc_base[:cut_len] + "..."
                        html_combined = f"{esc_base}\n\n{html_link}"
                else:
                    html_combined = html_link

                await bot.edit_message(
                    target_chat_id, target_msg_id,
                    text=html_combined,
                    parse_mode="html",
                    link_preview=False,
                )
                logger.info("私聊来源链接 HTML 回退追加成功: target_msg=%s", target_msg_id)
            except Exception as e2:
                logger.warning("私聊追加来源链接失败: target_msg=%s, err=%s", target_msg_id, e2)

    async def _forward_private_message(event, fetch_chat_id: int, msg,
                                       parsed: ParsedLink,
                                       client: TelegramClient | None = None,
                                       source_url: str | None = None):
        """私聊链接解析后的统一转发逻辑。"""
        ub = client or userbot
        reply_to_id = event.message.id
        kind = classify_message_kind(msg, single=parsed.single)
        if kind == "album":
            album_msgs = await collect_album_messages(
                ub, fetch_chat_id, msg, window=10)
            logger.info("媒体集合: %d 条, grouped_id=%s",
                        len(album_msgs), msg.grouped_id)
            source_ids = [m.id for m in album_msgs]
            target_ids = await forwarder.forward_album(
                fetch_chat_id, source_ids, event.chat_id, mode="copy",
                target_topic_id=reply_to_id, caller="private", userbot=ub)
            if not target_ids:
                await event.reply("❌ 转发失败，已尝试所有策略")
                return

            if append_source_link and source_url and target_ids:
                # 寻找原相册中带有 caption 的索引，若全无则默认取第 1 条 (index 0)
                caption_idx = 0
                for idx, m in enumerate(album_msgs):
                    if m.text:
                        caption_idx = idx
                        break
                target_msg_id = target_ids[caption_idx] if caption_idx < len(target_ids) else target_ids[0]
                base_text = album_msgs[caption_idx].text if caption_idx < len(album_msgs) else ""
                await _append_link_to_target(event.chat_id, target_msg_id, base_text or "", source_url, is_media=True)
            return

        if kind in ("media", "text"):
            if kind == "media":
                logger.info("单条媒体消息")
            else:
                logger.info("纯文本消息")
            target_id = await forwarder.forward_message(
                fetch_chat_id, msg.id, event.chat_id, mode="copy",
                target_topic_id=reply_to_id, caller="private", userbot=ub)
            if not target_id:
                await event.reply("❌ 转发失败，已尝试所有策略")
                return

            if append_source_link and source_url and target_id:
                is_media = (kind == "media")
                await _append_link_to_target(event.chat_id, target_id, msg.text or "", source_url, is_media=is_media)
            return

        await event.reply("❌ 消息内容为空")

    async def _parse_private_link_and_source(
        event,
    ) -> tuple[ParsedLink, int] | None:
        link = extract_tg_link(event.raw_text)
        if not link:
            return None

        parsed = parse_link(link)
        if not parsed or not parsed.msg_id:
            await event.reply("❌ 无法解析链接，需要包含消息ID")
            return None

        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问该频道/群组")
            return None

        return parsed, source_id

    async def _fetch_private_target_message(event, fetch_target: FetchTarget,
                                            client: TelegramClient | None = None):
        ub = client or userbot
        try:
            msg = await ub.get_messages(fetch_target.chat_id, ids=fetch_target.msg_id)
            if not msg:
                await event.reply("❌ 消息不存在")
                return None
            return msg
        except ValueError as e:
            if "input entity" in str(e):
                await event.reply("❌ UserBot 未加入该私有频道/群组，无法访问")
            else:
                await event.reply(f"❌ 获取消息失败: {e}")
            logger.warning("私聊解析失败: %s", e)
        except errors.ChannelPrivateError:
            await event.reply("❌ 该频道/群组为私有，UserBot 未加入无法访问")
        except Exception as e:
            logger.warning("私聊解析失败: %s", e)
            await event.reply(f"❌ 获取消息失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @bot.on(events.NewMessage(pattern=r"/(?:start|menu)(?:@\w+)?$"))
    async def on_start(event):
        logger.info("收到 /start 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        await menu.show_main_menu(event)

    @bot.on(events.NewMessage(pattern=r"/help(?:@\w+)?$"))
    async def on_help(event):
        await event.reply(
            "📖 命令与功能对照\n\n"
            "【克隆 / 监听】\n"
            "/kl <链接> — 克隆该频道全部消息到本群，完成后自动持续监听\n"
            "/kl 100-1100 <链接> — 只克隆 msg 100~1100（含两端，区间也可写在链接后）\n"
            "/kljx <链接> — 接着上次进度补到最新，已克隆过的不会重复\n"
            "/kljx <链接> 120 — 从消息 120 开始继续克隆到最新\n"
            "/kljx <链接> 120-1000 — 只克隆 msg 120~1000\n"
            "/jt <链接> — 只监听新消息（不克隆历史）\n"
            "/kllb — 克隆任务列表（含已完成，记录永久保留）\n"
            "/jtlb — 监听任务列表\n"
            "/klfail — 查看哪些任务有失败（带 ID 看明细）\n"
            "/retryfail <任务id|all> — 一键补发失败条目，补齐缺口\n\n"
            "【工具】\n"
            "/chapd — 转发一条频道消息到本群，识别来源频道名称/ID/链接\n"
            "/chaid — 转发一条频道消息到本群，返回消息 ID 和消息链接（配 /kljx 用）\n"
            "/start 或 /menu — 按钮菜单（推荐，含进度、暂停、继续、开关持续监听）\n"
            "/settings — 查看限流与数据存储状态\n"
            "/help — 本说明\n\n"
            "【兼容旧命令】\n"
            "/clone [链接] — 进入克隆向导\n"
            "/sync <链接> [--forward] [--from-id N] [--from-time 2024-01-31]\n"
            "/syncrestrictedmsg <链接> — Takeout 补发受限消息\n"
            "/takeout <链接> — Takeout 解析单条消息/相册\n"
            "/monitor <链接> — 同 /jt\n"
            "/list — 任务列表\n\n"
            "链接格式：\n"
            "• https://t.me/channel 或 @channel\n"
            "• https://t.me/channel/123（从该条起）\n"
            "• https://t.me/c/123456/789（私有频道）\n"
            "• -1001234567890（直接用频道 ID）")

    @bot.on(events.NewMessage(pattern=r"^/clone(?:@\w+)?(?:\s+(.+))?$"))
    async def on_clone(event):
        """命令入口：直接进入克隆向导（可选带源链接）。"""
        if not await _require_admin(event):
            return
        raw = (event.pattern_match.group(1) or "").strip()
        await menu.start_wizard_via_command(event, "clone", raw or None)

    # ------------------------------------------------------------------
    # 新版命令族：/kl /jt /kllb /jtlb /kljx /chapd
    # ------------------------------------------------------------------

    _chapd_wait: dict[tuple[int, int], float] = {}
    # /chaid：等一条转发消息，用于获取源频道的「消息 ID + 消息链接」
    _chaid_wait: dict[tuple[int, int], float] = {}

    async def _resolve_cmd_source(event, raw: str):
        """命令里解析源：复用菜单的解析逻辑，错误直接回复用户。"""
        link = extract_tg_link(raw) or raw.strip()
        if not link:
            await event.reply("❌ 请提供频道链接或 @用户名")
            return None
        info, err = await _menu_resolve_source(event, link)
        if not info:
            await event.reply(f"❌ {err or '无法解析该频道'}")
            return None
        return info

    async def _latest_source_msg_id(source_id: int, topic_id=None,
                                    client=None) -> int | None:
        """取源（含话题）最新一条消息 ID；无法判定时返回 None。

        None 表示"查不到"（客户端无权限/网络异常），调用方应按"无法判断"处理，
        不能当成"没有新消息"，否则会误判成已是最新而漏补。
        """
        return await latest_source_msg_id(source_id, topic_id, client, userbot, bot)

    @bot.on(events.NewMessage(pattern=r"^/kl(?:@\w+)?(?=\s|$)"))
    async def on_kl(event):
        """`/kl <链接>` 克隆全部；`/kl 100-1100 <链接>` 只克隆区间；完成后持续监听。"""
        if not await _require_admin(event):
            return
        raw = strip_command(event.raw_text, "kl")
        start_id, end_id, rest, range_desc = parse_kljx_args(raw)
        if not rest:
            await event.reply(
                "用法：\n"
                "• `/kl https://t.me/xxx` — 克隆该频道全部消息，完成后持续监听\n"
                "• `/kl 100-1100 https://t.me/xxx` — 只克隆 msg 100~1100\n"
                "• `/kl https://t.me/xxx 100-1100` — 区间写在链接后也可以\n"
                "• `/kljx https://t.me/xxx` — 接着上次进度补到最新（不重复）\n"
                "• `/kljx https://t.me/xxx 120` — 从消息 120 开始继续克隆")
            return

        info = await _resolve_cmd_source(event, rest)
        if not info:
            return

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        target_title = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True)
        start_desc = range_desc or "从头开始（最早 → 最新）"

        task_id = await _create_clone_task(
            source_id=info["chat_id"],
            target_chat_id=target_chat_id,
            mode="copy",
            source_topic_id=info.get("topic_id"),
            target_topic_id=target_topic_id,
            start_msg_id=start_id,
            end_msg_id=end_id,
            follow=1,
            source_title=info.get("title"),
            target_title=target_title,
            notify_chat_id=event.chat_id,
            created_by=event.sender_id,
            userbot=info.get("userbot"),
            start_desc=start_desc,
        )
        await event.reply(
            f"✅ 克隆任务 #{task_id} 已创建\n"
            f"📌 源: {info.get('title')}\n"
            f"📍 范围: {start_desc}\n"
            f"🛰 跑完后自动持续监听新消息（可在任务详情里关闭）",
            buttons=[[Button.inline("📋 任务详情", data=f"m:task:{task_id}"),
                      Button.inline("📥 克隆列表", data="m:tasks")]])

    @bot.on(events.NewMessage(pattern=r"^/jt(?:@\w+)?(?=\s|$)"))
    async def on_jt(event):
        """`/jt <链接>` 只监听新消息，不克隆历史。"""
        if not await _require_admin(event):
            return
        raw = strip_command(event.raw_text, "jt")
        if not raw:
            await event.reply("用法：`/jt https://t.me/xxx` — 只监听该频道的新消息并克隆到本群")
            return
        info = await _resolve_cmd_source(event, raw)
        if not info:
            return

        ok, reason, ub = await _ensure_userbot_joined_source_for_monitor(
            info["chat_id"], info.get("topic_id"))
        if not ok:
            await event.reply(reason or "❌ UserBot 未加入该源，无法监听")
            return

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        target_title = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True)
        task_id, err, created = await _create_monitor_task(
            source_id=info["chat_id"],
            target_chat_id=target_chat_id,
            mode="copy",
            source_topic_id=info.get("topic_id"),
            target_topic_id=target_topic_id,
            source_title=info.get("title"),
            target_title=target_title,
            notify_chat_id=event.chat_id,
            created_by=event.sender_id,
            userbot=ub or info.get("userbot"))
        if not task_id:
            await event.reply(f"❌ {err or '创建监听任务失败'}")
            return
        if not created:
            await event.reply(
                f"ℹ️ 该监听已存在：任务 #{task_id}，未重复创建。\n"
                "发 /jtlb 查看监听列表。")

    @bot.on(events.NewMessage(pattern=r"^/kllb(?:@\w+)?$"))
    async def on_clone_list(event):
        """克隆任务列表（含已完成的，记录永久保留）。"""
        if not await _require_admin(event):
            return
        await menu.render_tasks(event, edit=False)

    @bot.on(events.NewMessage(pattern=r"^/jtlb(?:@\w+)?$"))
    async def on_monitor_list(event):
        """监听任务列表。"""
        if not await _require_admin(event):
            return
        await menu.render_monitors(event, edit=False)

    @bot.on(events.NewMessage(pattern=r"^/kljx(?:@\w+)?(?=\s|$)"))
    async def on_kl_continue(event):
        """`/kljx <链接> [ID|区间]` 继续克隆。

        不带 ID：从上次游标补到最新，已克隆过的不会重复；
        带 ID（如 `/kljx <链接> 120`）：把续跑点改到消息 120，从那里克隆到最新
        （用于游标之后漏了消息、需要手动指定起点重补的场景）；
        带区间（如 `/kljx <链接> 120-1000`）：从 120 克隆到 1000。
        """
        if not await _require_admin(event):
            return
        raw = strip_command(event.raw_text, "kljx")
        if not raw:
            await event.reply(
                "用法：\n"
                "• `/kljx https://t.me/xxx` — 接着上次进度继续克隆到最新\n"
                "• `/kljx https://t.me/xxx 120` — 从消息 120 开始克隆到最新\n"
                "• `/kljx https://t.me/xxx 120-1000` — 只克隆 msg 120~1000\n"
                "（起始 ID 可用 `/chaid` 转发一条消息自动获取）")
            return
        start_id, end_id, rest, range_desc = parse_kljx_args(raw)
        if not rest:
            await event.reply("❌ 没有识别到频道链接，请检查格式：`/kljx https://t.me/xxx 120`")
            return
        info = await _resolve_cmd_source(event, rest)
        if not info:
            return

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        found = await models.find_clone_task(
            db, info["chat_id"], target_chat_id,
            info.get("topic_id"), target_topic_id)
        if not found:
            await event.reply(
                "📭 没有找到「该频道 → 本群」的克隆记录。\n"
                "第一次请用 `/kl <链接>` 新建；已有记录后再用 `/kljx` 补新消息。")
            return

        cursor = int(found.get("last_synced_msg_id") or 0)
        follow = int(found.get("follow") or 0)

        # 带起始 ID：显式改写续跑点，跳过「已是最新」判断（用户就是要从那里重来）
        if start_id > 0:
            if end_id and end_id < start_id:
                start_id, end_id = end_id, start_id
            await models.set_clone_resume_point(
                db, found["id"], start_msg_id=start_id, end_msg_id=end_id)
            ok, err = await _resume_clone(found["id"], allow_completed=True)
            if not ok:
                await models.update_task_status(db, found["id"], "paused")
                await event.reply(f"❌ 继续克隆失败：{err or '未知原因'}")
                return
            range_line = (f"到消息 {end_id}" if end_id else "到最新消息")
            await event.reply(
                f"🔁 已从消息 {start_id} 开始继续：任务 #{found['id']}\n"
                f"• 范围: msg {start_id} → {range_line}\n"
                f"• 原游标 msg_id={cursor} 已被改写；这段若克隆过会**重复发送**\n"
                f"• {'补完后自动持续监听' if follow else '需要持续跟新可在任务详情里开启 🛰 持续监听'}",
                buttons=[[Button.inline("📋 任务详情", data=f"m:task:{found['id']}")]])
            return

        # 先看一眼源的最新消息：已经追平就不用空跑一遍
        latest = await _latest_source_msg_id(
            info["chat_id"], info.get("topic_id"), info.get("userbot"))
        if latest is not None and cursor and latest <= cursor:
            if follow:
                await _ensure_follow_monitor(found["id"], notify=False)
            await event.reply(
                f"✅ 已是最新，没有需要补的消息\n"
                f"• 上次游标: msg_id={cursor}（源最新 msg_id={latest}）\n"
                + ("• 持续监听仍在运行，后续新消息会自动转发\n" if follow
                   else "• 需要跟着转发新消息，可在任务详情里开启 🛰 持续监听\n")
                + "克隆记录继续保留，不会被清掉。",
                buttons=[[Button.inline("📋 任务详情", data=f"m:task:{found['id']}")]])
            return

        await models.reopen_clone_task(db, found["id"], follow=follow, clear_end=True)
        ok, err = await _resume_clone(found["id"], allow_completed=True)
        if not ok:
            await models.update_task_status(db, found["id"], "paused")
            await event.reply(f"❌ 继续克隆失败：{err or '未知原因'}")
            return

        tail = ("• 补完后自动持续监听" if follow
                else "• 需要持续跟新可在任务详情里开启 🛰 持续监听")
        await event.reply(
            f"🔁 已从断点继续：任务 #{found['id']}\n"
            f"• 上次游标: msg_id={cursor}\n"
            f"• 只补 msg {cursor} 之后的新消息，已克隆过的不会重复\n"
            f"{tail}",
            buttons=[[Button.inline("📋 任务详情", data=f"m:task:{found['id']}")]])

    @bot.on(events.NewMessage(pattern=r"^/klfail(?:@\w+)?(?=\s|$)"))
    async def on_clone_failures(event):
        """失败清单：`/klfail` 看哪些任务有失败，`/klfail <id>` 看明细。"""
        if not await _require_admin(event):
            return
        raw = strip_command(event.raw_text, "klfail").strip()

        if raw.isdigit():
            task_id = int(raw)
            if not await models.get_task(db, task_id):
                await event.reply(f"❌ 任务 #{task_id} 不存在")
                return
            rows = await models.list_failed_messages(db, task_id, limit=30)
            total = await models.count_failed_messages(db, task_id)
            if not rows:
                await event.reply(f"✅ 任务 #{task_id} 没有失败记录")
                return
            lines = [f"⚠️ 任务 #{task_id} 失败明细（共 {total} 条，显示前 {len(rows)} 条）"]
            for r in rows:
                reason = (r.get("reason") or "未记录原因").replace("\n", " ")
                lines.append(
                    f"• msg {r['source_msg_id']} · 第 {r.get('attempts') or 1} 次"
                    f" · {truncate(reason, 40)}")
            lines.append(f"\n补发：`/retryfail {task_id}`")
            await event.reply("\n".join(lines))
            return

        tasks = await models.get_tasks_with_failures(db, limit=10)
        if not tasks:
            await event.reply("✅ 没有失败记录，全部克隆都完整落地了")
            return
        lines = ["⚠️ 有失败记录的任务"]
        for t in tasks:
            src = t.get("source_title") or str(t["source_chat_id"])
            tgt = t.get("target_title") or str(t["target_chat_id"])
            lines.append(f"• #{t['id']} {truncate(src, 18)} → {truncate(tgt, 14)}"
                         f" · 失败 {t['failed_n']} 条")
        lines.append("\n查看明细：`/klfail <任务id>`　一键补发：`/retryfail <任务id>`")
        await event.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/retryfail(?:@\w+)?(?=\s|$)"))
    async def on_retry_failed(event):
        """按失败清单补发：`/retryfail <任务id>` 或 `/retryfail all`。"""
        if not await _require_admin(event):
            return
        raw = strip_command(event.raw_text, "retryfail").strip()
        if raw not in ("all", "全部") and not raw.isdigit():
            await event.reply(
                "用法：\n"
                "• `/retryfail 12` — 补发任务 #12 的失败条目\n"
                "• `/retryfail all` — 补发所有任务的失败条目\n"
                "（先 `/klfail` 看有哪些任务存在缺口）")
            return

        if raw.isdigit():
            targets = [int(raw)]
        else:
            targets = [t["id"] for t in await models.get_tasks_with_failures(db, limit=20)]
        if not targets:
            await event.reply("✅ 没有需要补发的失败记录")
            return

        await event.reply(
            f"🔁 开始补发 {len(targets)} 个任务的失败条目…"
            + ("\n⏳ 已有克隆在跑，等它跑完自动开始" if _clone_gate.full else ""))

        total_ok = total_fail = 0
        details: list[str] = []
        async with _clone_gate:
            for tid in targets:
                try:
                    ok, still = await syncer.retry_failed(tid)
                except Exception as e:
                    logger.warning("补发任务 #%s 失败: %s", tid, e)
                    details.append(f"• #{tid} 补发出错：{type(e).__name__}")
                    continue
                total_ok += ok
                total_fail += still
                if ok or still:
                    details.append(f"• #{tid} 补成功 {ok} 条 · 仍失败 {still} 条")

        text = [f"🔁 补发结束：成功 {total_ok} 条，仍失败 {total_fail} 条"]
        text.extend(details[:15])
        if total_fail:
            text.append("仍失败的条目通常是源消息已删除、超出体积上限或仍被限制。")
        await event.reply("\n".join(text))

    def _speed_status(rl: dict, overrides: dict | None = None) -> str:
        def rng(v) -> str:
            if isinstance(v, list):
                return f"{v[0]}~{v[1]} 秒"
            return f"{v} 秒"
        bi = rl.get("forward_interval", [1, 2])
        be = rl.get("batch_pause_every", 50)
        bt = rl.get("batch_pause_time", [30, 60])
        ui = rl.get("userbot_forward_interval", [3, 6])
        ue = rl.get("userbot_forward_pause_every", 25)
        ut = rl.get("userbot_forward_pause_time", [60, 120])
        ov_line = ""
        if overrides:
            ov_line = (f"\n🟢 在线覆盖生效中: {'、'.join(sorted(overrides))}"
                       "（优先于 config.yaml，重启自动恢复）\n")
        return (
            "⚙️ 当前克隆速度阈值（改动立即生效，无需重启）\n"
            f"【Bot】每条间隔 {rng(bi)} · 每 {be} 条歇 {rng(bt)}\n"
            f"【UserBot】每条间隔 {rng(ui)} · 每 {ue} 条歇 {rng(ut)}\n"
            f"{ov_line}\n"
            "用法：\n"
            "• /sd bot fast — Bot 提速（0.3~0.8s/条，每 100 条歇 20~40s）\n"
            "• /sd bot max — Bot 全速（基本不等待，易触发 FloodWait，慎用）\n"
            "• /sd bot safe — 恢复默认\n"
            "• /sd bot 0.5 或 /sd bot 0.3-0.8 — 自定义每条间隔\n"
            "• /sd bot pause 100 20-40 — 每 100 条歇 20~40 秒\n"
            "• /sd ub fast / safe / 3-6 / pause 25 60-120 — 同上，作用于 UserBot\n\n"
            "💡 Bot 走官方服务端转发、阈值较高，fast 档一般很安全；\n"
            "   UserBot 是用户账号，建议别低于 1.5s/条——被风控的代价比慢高得多。"
        )

    @bot.on(events.NewMessage(pattern=r"^/sd(?:@\w+)?(?=\s|$)"))
    async def on_speed(event):
        """查看/设置克隆转发速度阈值（数据库持久化，所有限流器立即生效）。"""
        if not await _require_admin(event):
            return
        rest = event.raw_text.split(maxsplit=1)
        arg = rest[1].strip() if len(rest) > 1 else ""
        action, payload = parse_speed_args(arg)
        if action == "error":
            await event.reply(payload)
            return

        async def _current_overrides() -> dict:
            raw = await models.get_setting(db, OVERRIDE_KEY)
            return normalize_overrides(json.loads(raw)) if raw else {}

        def _effective(ov: dict) -> dict:
            rl = dict(_get_dynamic_rate_limit(config))
            rl.update(ov)
            return rl

        if action == "show":
            ov = await _current_overrides()
            await event.reply(_speed_status(_effective(ov), ov))
            return

        # 覆盖值持久化到数据库（data 目录挂载到宿主机，重启不丢），
        # 并立即作用到所有 RateLimiter 实例
        merged = merge_overrides(await _current_overrides(), payload)
        try:
            await models.set_setting(db, OVERRIDE_KEY, json.dumps(merged))
        except Exception as e:
            await event.reply(f"❌ 保存速度设置失败：{e}")
            return
        for rl in _rate_limiters:
            rl.set_overrides(merged)

        # config.yaml 能写就顺手同步（便于人工查看），只读则忽略
        cfg_note = ""
        try:
            apply_speed("config.yaml", payload)
        except OSError:
            cfg_note = "\nℹ️ config.yaml 为只读，不影响使用：设置已存数据库并立即生效。"

        changed = "、".join(payload)
        await event.reply(
            f"✅ 已更新（{changed}），立即生效\n\n"
            + _speed_status(_effective(merged), merged)
            + cfg_note)

    @bot.on(events.NewMessage(pattern=r"^/cfg(?:@\w+)?(?=\s|$)"))
    async def on_cfg(event):
        """查看/设置配置覆盖值（app_settings 持久化，config.yaml 只读也可用）。"""
        if not await _require_admin(event):
            return
        parts = event.raw_text.split(maxsplit=2)
        overlay = await async_load_overlay(db)

        if len(parts) == 1:
            if not overlay:
                await event.reply(
                    "🧩 当前没有配置覆盖项（一切以 config.yaml 为准）\n\n"
                    "用法：\n"
                    "• /cfg <段.键> <值> — 覆盖某项配置，如 "
                    "/cfg transfer.strategy_timeout 900\n"
                    "• /cfg <段.键> reset — 恢复 config.yaml 的值\n"
                    "• 值支持 YAML 标量语法：数字 / true / [1, 2]\n\n"
                    "💡 覆盖值存在数据库（data/bot.db），重启不丢；\n"
                    "   多数配置项在组件启动时读取，重启后完全生效。")
                return
            lines = ["🧩 配置覆盖项（优先于 config.yaml，重启不丢）"]
            lines.extend(f"• {p} = {overlay[p]}" for p in sorted(overlay))
            lines.append("\n/cfg <段.键> reset 可恢复 config.yaml 的值")
            await event.reply("\n".join(lines))
            return

        arg = parts[1].strip()
        if "." not in arg:
            await event.reply("路径应为「段.键」，如 transfer.strategy_timeout")
            return
        if len(parts) < 3:
            await event.reply(f"缺少值：/cfg {arg} <值>（或 reset）")
            return

        value_txt = parts[2].strip()
        if value_txt.lower() in ("reset", "恢复", "默认"):
            if arg not in overlay:
                await event.reply(f"「{arg}」没有覆盖记录，本来就以 config.yaml 为准")
                return
            overlay.pop(arg)
            await async_save_overlay(db, overlay)
            # 内存 config 同步回退到 config.yaml 磁盘值
            sec, key = arg.split(".", 1)
            try:
                with open("config.yaml", encoding="utf-8") as f:
                    disk = yaml.safe_load(f) or {}
                disk_val = (disk.get(sec) or {}).get(key)
                if isinstance(config.get(sec), dict):
                    config[sec][key] = disk_val
            except OSError:
                pass
            await event.reply(
                f"✅ 已移除「{arg}」的覆盖，恢复以 config.yaml 为准（重启不丢）")
            return

        value = parse_value(value_txt)
        overlay[arg] = value
        await async_save_overlay(db, overlay)
        apply_overlay(config, overlay)
        await event.reply(
            f"✅ 已覆盖 {arg} = {value}（已持久化到数据库，重启不丢）\n"
            "多数配置项在组件启动时读取，重启后完全生效。")

    # ── 代理测速命令：/speedall（全量榜） / /speedtop（前 40 名）──────────
    _speedtest_busy = False

    async def _run_proxy_speedtest(event, kind: str) -> None:
        """拉取榜单 → 本机测速 → 选优写状态 → 自动重启应用。"""
        nonlocal _speedtest_busy
        if not await _require_admin(event):
            return
        if _speedtest_busy:
            await event.reply("⏳ 已有一轮测速在进行中，等它结束后再试")
            return

        proxy_cfg = config.get("proxy") or {}
        if proxy_cfg.get("mode") == "off":
            await event.reply(
                "当前 proxy.mode=off（直连），测速结果不会生效。\n"
                "如需使用，请先把 config.yaml 中 proxy.mode 改为 auto。")
            return
        if not (proxy_cfg.get("sources") or []) and proxy_cfg.get("mode") != "auto":
            await event.reply(
                f"当前 proxy.mode={proxy_cfg.get('mode') or 'auto'}，"
                "测速结果仍会写入状态文件，但只有 auto 模式重启后才会使用它。")

        kind_label = "全量榜" if kind == "all" else "前 40 名"
        _speedtest_busy = True
        progress_msg = await event.reply(
            f"🚀 开始测速（{kind_label}）…\n拉取榜单中，测速完成后自动选用最快的代理")
        try:
            async def _progress(done: int, total: int, alive: int) -> None:
                try:
                    await progress_msg.edit(
                        f"🚀 测速中（{kind_label}）…\n"
                        f"进度: {done}/{total} · 存活 {alive}")
                except Exception:
                    pass  # 编辑失败（限流/内容未变）不影响测速本身

            from core.proxy_speedtest import run_speed_test
            rep = await run_speed_test(kind, proxy_cfg, progress_cb=_progress)
        except RuntimeError as e:
            await event.reply(f"❌ 测速失败：{e}")
            return
        except Exception as e:
            logger.exception("代理测速异常 kind=%s", kind)
            await event.reply(f"❌ 测速异常：{type(e).__name__}: {e}")
            return
        finally:
            _speedtest_busy = False

        best = rep["best"]
        mins = int(rep["elapsed"] // 60)
        secs = rep["elapsed"] % 60
        top_lines = []
        for i, c in enumerate(
                [x for x in rep["ranked"] if x.get("dc_ok")][:5], 1):
            top_lines.append(
                f"{i}. {c['server']}:{c['port']} · DC {c['dc_ms']}ms · 握手 "
                f"{c['latency_ms']}ms")
        text = (
            f"🏁 测速完成（{kind_label}）\n"
            f"• 共测 {rep['total']:,} 条 · 存活 {rep['alive']:,} · "
            f"超时/不可用 {rep['failed']:,}\n"
            f"• 用时 {mins}分{secs:.0f}秒\n"
            f"🥇 最优: {best['server']}:{best['port']}"
            f"（握手 {best['latency_ms']}ms · DC 往返 {best.get('dc_ms') or '—'}ms）")
        if top_lines:
            text += "\nTop5:\n" + "\n".join(top_lines)
        text += f"\n✅ 已写入 {rep['state_file']}，设为当前使用的代理"

        if _sync_tasks:
            await event.reply(
                text + "\n\n⚠️ 当前有克隆任务在跑，为免中断未自动重启。\n"
                       "任务结束后重启容器即按新代理连接。")
            return
        await event.reply(text + "\n🔄 10 秒后自动重启进程以应用新代理…")
        async def _apply_restart() -> None:
            await asyncio.sleep(10)
            logger.info("测速选优完成，重启进程应用新代理")
            os._exit(0)
        asyncio.create_task(_apply_restart())

    @bot.on(events.NewMessage(pattern=r"^/(speedall|speedtop)(?:@\w+)?(?=\s|$)"))
    async def on_proxy_speedtest(event):
        """对代理榜单做本机测速并自动选用最快的代理。"""
        kind = "all" if event.pattern_match.group(1) == "speedall" else "top40"
        await _run_proxy_speedtest(event, kind)

    @bot.on(events.NewMessage(pattern=r"^/bf(?:@\w+)?(?=\s|$)"))
    async def on_clone_gate(event):
        """查看/设置克隆并发数：/bf 查看，/bf 2 设置（立即生效，重启不丢）。"""
        if not await _require_admin(event):
            return
        parts = event.raw_text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        def _status() -> str:
            state = "🟢 空闲" if _clone_gate.active == 0 else (
                f"🏃 {min(_clone_gate.active, _clone_gate.limit)} 个在跑"
                + (f" · {_clone_gate.active - _clone_gate.limit} 个排队"
                   if _clone_gate.active > _clone_gate.limit else ""))
            return (
                "🚦 克隆并发设置\n"
                f"• 同时克隆上限: {_clone_gate.limit}\n"
                f"• 当前状态: {state}\n\n"
                "用法：/bf <数量>（1~10），如 /bf 2\n\n"
                "⚠️ 并发调大 = 请求频率成倍叠加：Bot 侧多表现为 FloodWait 变慢，\n"
                "   UserBot 侧则直接放大风控风险（所有任务共用同一个用户账号）。\n"
                "   建议保持 1 个，确有需要再用 2；配合 /sd ub safe 更稳。")

        if not arg:
            await event.reply(_status())
            return

        if not arg.isdigit():
            await event.reply("并发数量应为 1~10 的整数，如 /bf 2")
            return
        n = int(arg)
        if not 1 <= n <= 10:
            await event.reply("并发数量范围是 1~10。对个人使用来说，超过 2 "
                              "基本只有风控风险没有速度收益。")
            return

        old = _clone_gate.limit
        await _clone_gate.set_limit(n)
        # 持久化：走通用配置覆盖层，重启不丢
        try:
            overlay = await async_load_overlay(db)
            overlay["clone.max_concurrent"] = n
            await async_save_overlay(db, overlay)
            clone_cfg = config.setdefault("clone", {})
            if isinstance(clone_cfg, dict):
                clone_cfg["max_concurrent"] = n
        except Exception as e:
            await event.reply(
                f"✅ 并发上限已调整为 {n}（本次运行生效），"
                f"但持久化失败：{e}")
            return

        tip = ""
        if n > old:
            tip = f"\nℹ️ 已从 {old} 调大：排队中的任务会自动开始。"
        elif n < old:
            tip = "\nℹ️ 已调小：正在跑的任务不受影响，多余的排队等待释放。"
        await event.reply(f"✅ 克隆并发上限 {old} → {n}，立即生效、重启不丢\n" + tip
                          + "\n\n" + _status())

    @bot.on(events.NewMessage(pattern=r"^/chapd(?:@\w+)?$"))
    async def on_chapd(event):
        """进入「等一条转发消息」状态，用来识别来源频道。"""
        if not await _require_admin(event):
            return
        _chapd_wait[(event.chat_id, event.sender_id)] = time.time()
        await event.reply(
            "📡 请把目标频道里的任意一条消息**转发**到本群（用「转发」，不要复制文本）。\n"
            "我会返回该频道的名称、@用户名、ID 和可直接用于 /kl 的链接。\n"
            f"（{CHAPD_TTL // 60} 分钟内有效）")

    @bot.on(events.NewMessage(func=lambda e: (
        getattr(e.message, "fwd_from", None) is not None
        and (_chapd_wait.get((e.chat_id, e.sender_id)) or 0) > time.time() - CHAPD_TTL
    )))
    async def on_chapd_forward(event):
        if not await _require_admin(event):
            return
        _chapd_wait.pop((event.chat_id, event.sender_id), None)
        info = describe_forwarded_source(event.message)
        if not info:
            await event.reply("⚠️ 这条消息里没有可识别的频道来源，请确认是从频道转发（不是复制文本）后重试。")
            return
        username_line = f"@{info['username']}" if info["username"] else "无（私有频道）"
        await event.reply(
            "📡 已识别来源频道\n"
            f"• 名称: {info['title']}\n"
            f"• 用户名: {username_line}\n"
            f"• ID: {info['chat_id']}\n"
            f"• 链接: {info['link']}\n\n"
            "可直接复制下面的命令：\n"
            f"`/kl {info['link']}` — 克隆全部并持续监听\n"
            f"`/kljx {info['link']}` — 只补新消息\n"
            f"`/jt {info['link']}` — 只监听新消息\n"
            f"`/kl {info['chat_id']}` — 用频道 ID 直接克隆")

    @bot.on(events.NewMessage(pattern=r"^/chaid(?:@\w+)?$"))
    async def on_chaid(event):
        """进入「等一条转发消息」状态，返回该消息的链接与 ID，配合 /kljx 用。"""
        if not await _require_admin(event):
            return
        _chaid_wait[(event.chat_id, event.sender_id)] = time.time()
        await event.reply(
            "📎 请把目标频道里的**任意一条消息转发**到本群（用「转发」，不要复制文本）。\n"
            "我会返回这条消息在源频道里的**消息 ID** 和**消息链接**，可直接用于：\n"
            "`/kljx <链接> <消息ID>` — 从这条开始继续克隆\n"
            f"（{CHAPD_TTL // 60} 分钟内有效）")

    @bot.on(events.NewMessage(func=lambda e: (
        getattr(e.message, "fwd_from", None) is not None
        and (_chaid_wait.get((e.chat_id, e.sender_id)) or 0) > time.time() - CHAPD_TTL
    )))
    async def on_chaid_forward(event):
        if not await _require_admin(event):
            return
        _chaid_wait.pop((event.chat_id, event.sender_id), None)
        msg = event.message
        info = describe_forwarded_source(msg)
        post_id = extract_forwarded_msg_id(msg)
        if not info or not post_id:
            await event.reply(
                "⚠️ 没有识别到源频道的消息 ID。\n"
                "请直接在频道里对某条消息按「转发」→ 选本群（转发个人消息或复制文本\n"
                "都拿不到频道消息 ID），再发送一次 /chaid 重试。")
            return
        msg_link = build_message_link(info["chat_id"], info["username"], post_id)
        await event.reply(
            "📎 已识别源消息\n"
            f"• 频道: {info['title']}\n"
            f"• 消息 ID: {post_id}\n"
            f"• 消息链接: {msg_link}\n\n"
            "可直接复制下面的命令：\n"
            f"`/kljx {info['link']} {post_id}` — 从这条开始继续克隆到最新\n"
            f"`/kl {info['link']} {post_id}-{post_id}` — 只克隆这一条\n"
            f"`/kljx {info['link']} {post_id}-999999` — 从这条克隆到接近最新")

    @bot.on(events.NewMessage(pattern=r"/sync(?:@\w+)?\s+"))
    async def on_sync(event):
        logger.info("收到 /sync 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id
        mode = result.mode

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)

        active_ub = None
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(source_id)
            if not active_ub and parsed.is_private:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组'}")
                return

        # 可选起点：--from-id N / --from-time 2024-01-31[T12:30]
        start_msg_id, start_date, start_desc = 0, None, "从头开始（最早 → 最新）"
        raw = event.raw_text or ""
        m = re.search(r"--from-id\s+(\d+)", raw)
        if m:
            start_msg_id = int(m.group(1))
            start_desc = f"从指定消息 {start_msg_id} 开始"
        else:
            m = re.search(
                r"--from-time\s+(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?)",
                raw)
            if m:
                when = parse_start_time(m.group(1))
                if not when:
                    await event.reply("❌ --from-time 时间格式无法识别，例如：2024-01-31 或 2024-01-31T12:30")
                    return
                start_date = when.isoformat()
                start_desc = f"从 {when.strftime('%Y-%m-%d %H:%M')} 之后开始"

        logger.info("创建克隆任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s "
                    "start_msg=%s start_date=%s (UserBot: %s)",
                    source_id, parsed.topic_id, target_chat_id, target_topic_id, mode,
                    start_msg_id, start_date, getattr(active_ub, '_phone', 'default'))

        source_desc = await _format_chat_topic_display(source_id, parsed.topic_id)
        target_desc = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True
        )
        await _create_clone_task(
            source_id=source_id,
            target_chat_id=target_chat_id,
            mode=mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            start_msg_id=start_msg_id,
            start_date=start_date,
            source_title=source_desc,
            target_title=target_desc,
            notify_chat_id=event.chat_id,
            created_by=event.sender_id,
            userbot=active_ub,
            start_desc=start_desc,
        )

    @bot.on(events.NewMessage(pattern=r"/syncrestrictedmsg(?:@\w+)?\s+"))
    async def on_sync_restricted(event):
        logger.info("收到 /syncRestrictedMsg 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)

        active_ub = None
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(source_id)
            if not active_ub and parsed.is_private:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组'}")
                return

        logger.info(
            "创建受限同步任务: source=%s topic=%s -> target=%s target_topic=%s (UserBot: %s)",
            source_id, parsed.topic_id, target_chat_id, target_topic_id,
            getattr(active_ub, '_phone', 'default')
        )
        task_id = await models.create_task(
            db, "sync_restricted", source_id, target_chat_id, "copy",
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
        )

        rl = _get_dynamic_rate_limit(config)
        interval = rl.get("forward_interval", [2, 5])
        source_desc = await _format_chat_topic_display(source_id, parsed.topic_id)
        target_desc = await _format_chat_topic_display(
            target_chat_id, target_topic_id, prefer_bot=True
        )
        start_msg = await event.reply(
            f"🔓 受限消息同步任务 #{task_id} 已创建\n"
            f"📌 源: {source_desc}\n"
            f"🎯 目标: {target_desc}\n"
            f"⏱ 转发间隔: {interval[0]}-{interval[1]}秒/条\n"
            f"📋 模式: Takeout + Copy（仅同步受限消息）"
        )

        task = asyncio.create_task(restricted_syncer.start_sync(
            task_id, source_id, target_chat_id,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            notify_chat_id=event.chat_id,
            notify_topic_id=target_topic_id,
            notify_reply_to_msg_id=start_msg.id,
            userbot=active_ub,
        ))
        _sync_tasks[task_id] = task

    @bot.on(events.NewMessage(pattern=r"^/takeout(?:@\w+)?(?:\s+(.+))?$"))
    async def on_takeout(event):
        """通过 Takeout 导出接口解析单条链接或回复消息。"""
        if event.is_private:
            if not allow_public and not is_admin(event.sender_id):
                await event.reply("⛔ 无权限使用此命令")
                return
        else:
            if not await _require_admin(event):
                return

        link = extract_tg_link(event.raw_text)
        if not link and event.is_reply:
            reply = await event.get_reply_message()
            if reply and reply.raw_text:
                link = extract_tg_link(reply.raw_text)

        if not link:
            await event.reply("❌ 请提供有效的 Telegram 消息链接，或回复包含链接的消息。\n格式：`/takeout <链接>`")
            return

        parsed = parse_link(link)
        if not parsed or not parsed.msg_id:
            await event.reply("❌ 无法解析链接，链接中必须包含消息 ID")
            return

        source_id = await resolve_chat_id(userbot, parsed)
        if not source_id:
            await event.reply("❌ 无法访问源频道/群组")
            return

        fetch_target = await _resolve_private_fetch_target(parsed, source_id)
        if not fetch_target:
            await event.reply("❌ 无法获取讨论群实体，评论链接无法解析")
            return

        # 多 UserBot 按配置优先顺序探测（第 1 轮 Session 本地缓存命中，第 2 轮 dialogs 刷新命中）
        active_ub = userbot
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(
                fetch_target.chat_id, fetch_target.msg_id
            )
            if not active_ub:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组，无法访问'}")
                return

        phone = getattr(active_ub, "_phone", "default")
        ub_idx = getattr(active_ub, "_userbot_index", 1)
        source_desc = await _describe_source_for_display(source_id, parsed, client=active_ub)
        logger.info("收到 /takeout 命令: user=%s chat=%s 源=%s msg=%s (UserBot: #%d %s)",
                    event.sender_id, event.chat_id, source_desc, fetch_target.msg_id, ub_idx, phone)

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        real_chat_id = SyncComponentBase._ensure_supergroup_id(fetch_target.chat_id)
        source_url = _build_source_url(link, parsed, source_id)

        try:
            logger.info("正在启动 UserBot #%d [%s] 的 Takeout 会话...", ub_idx, phone)
            async with active_ub.takeout() as takeout:
                logger.info("Takeout 会话已建立，拉取目标消息 real_chat=%s msg_id=%s...",
                            real_chat_id, fetch_target.msg_id)
                msg = await takeout.get_messages(real_chat_id, ids=fetch_target.msg_id)
                if not msg:
                    await event.reply("❌ 消息不存在或无法通过 Takeout 获取")
                    return

                kind = classify_message_kind(msg, single=parsed.single)
                if kind == "album":
                    album_msgs = await collect_album_messages(
                        takeout, real_chat_id, msg, window=10
                    )
                    logger.info("Takeout 媒体集合: %d 条, grouped_id=%s",
                                len(album_msgs), msg.grouped_id)

                    if append_source_link and source_url:
                        caption_idx = 0
                        for idx, m in enumerate(album_msgs):
                            if m.text:
                                caption_idx = idx
                                break
                        base = album_msgs[caption_idx].text or ""
                        link_tag = f"[Source Link]({source_url})"
                        new_text = f"{base}\n\n{link_tag}" if base else link_tag
                        if len(new_text) > 1024:
                            overflow = len(new_text) - 1024
                            cut_len = max(0, len(base) - overflow - 3)
                            base = base[:cut_len] + "..."
                            new_text = f"{base}\n\n{link_tag}"
                        album_msgs[caption_idx].message = new_text
                        album_msgs[caption_idx]._text = new_text
                        album_msgs[caption_idx].entities = None

                    target_ids = await RestrictedSyncer._copy_album(
                        takeout, album_msgs, target_chat_id, target_topic_id
                    )
                    if not target_ids:
                        await event.reply("❌ Takeout 相册发送失败")
                    else:
                        logger.info("✅ Takeout 相册发送成功: %s", target_ids)
                    return

                if kind in ("media", "text"):
                    if append_source_link and source_url:
                        base = msg.text or ""
                        link_tag = f"[Source Link]({source_url})"
                        max_len = 1024 if is_file_media(msg) else 4096
                        new_text = f"{base}\n\n{link_tag}" if base else link_tag
                        if len(new_text) > max_len:
                            overflow = len(new_text) - max_len
                            cut_len = max(0, len(base) - overflow - 3)
                            base = base[:cut_len] + "..."
                            new_text = f"{base}\n\n{link_tag}"
                        msg.message = new_text
                        msg._text = new_text
                        msg.entities = None

                    target_id = await RestrictedSyncer._copy_single(
                        takeout, msg, target_chat_id, target_topic_id
                    )
                    if not target_id:
                        await event.reply("❌ Takeout 消息发送失败")
                    else:
                        logger.info("✅ Takeout 消息发送成功: %s", target_id)
                    return

                await event.reply("❌ 消息内容为空")

        except errors.TakeoutInitDelayError as e:
            logger.warning("Takeout 启动延迟: 需要等待 %s 秒", e.seconds)
            await event.reply(f"⚠️ Telegram Takeout 初始化限制：需等待 {e.seconds} 秒，或需在官方客户端确认导出授权。")
        except errors.ChatForwardsRestrictedError:
            logger.warning("Takeout 仍提示转发受限")
            await event.reply("❌ Takeout 转发失败：来源受限或目标群组无法接收")
        except errors.UserPrivacyRestrictedError:
            logger.warning("用户隐私设置限制私聊")
            await event.reply("❌ 发送失败：目标用户的隐私设置不允许 UserBot 发送私聊消息")
        except errors.FloodWaitError as e:
            logger.warning("Takeout 触发 FloodWait: %s秒", e.seconds)
            await event.reply(f"⏳ 触发 Telegram 频率限制，请等待 {e.seconds} 秒后重试")
        except Exception as e:
            logger.error("Takeout 执行异常: %s", e, exc_info=True)
            await event.reply(f"❌ Takeout 处理失败: {e}")

    @bot.on(events.NewMessage(pattern=r"/monitor(?:@\w+)?\s+"))
    async def on_monitor(event):
        logger.info("收到 /monitor 命令 from user=%s chat=%s", event.sender_id, event.chat_id)
        if not await _require_admin(event):
            return

        result = await _parse_source(event)
        if not result:
            return
        parsed = result.parsed
        source_id = result.source_id
        mode = result.mode

        target_chat_id = event.chat_id
        target_topic_id = get_target_topic_id(event)
        ok, reason, active_ub = await _ensure_userbot_joined_source_for_monitor(
            source_id, parsed.topic_id
        )
        if not ok:
            await event.reply(reason)
            return

        logger.info("创建监控任务: source=%s topic=%s -> target=%s target_topic=%s mode=%s (UserBot: %s)",
                     source_id, parsed.topic_id, target_chat_id, target_topic_id, mode,
                     getattr(active_ub, '_phone', 'default'))
        task_id, err, created = await _create_monitor_task(
            source_id=source_id,
            target_chat_id=target_chat_id,
            mode=mode,
            source_topic_id=parsed.topic_id,
            target_topic_id=target_topic_id,
            notify_chat_id=event.chat_id,
            created_by=event.sender_id,
            userbot=active_ub)
        if not task_id:
            await event.reply(f"❌ {err or '创建监控任务失败'}")
            return
        if not created:
            t = await models.get_task(db, task_id) or {}
            await event.reply(
                f"ℹ️ 该监控已存在：任务 #{task_id}（{t.get('status', 'unknown')}），未重复创建。\n"
                f"用 /list 查看已转发条数，或发 /menu 进「监控任务」管理。")

    @bot.on(events.NewMessage(pattern=r"/list(?:@\w+)?$"))
    async def on_list(event):
        if not await _require_admin(event):
            return

        tasks = await models.get_tasks_for_list(
            db, models.CLONE_TASK_TYPES + ("monitor",), limit=20)
        if not tasks:
            await event.reply("📭 还没有任务")
            return

        buttons = await _build_task_buttons(tasks)
        await event.reply("📋 任务列表（含已完成；点击管理）:", buttons=buttons)

    async def _show_task_detail(event, task_id: int):
        """渲染任务详情内联键盘（共享逻辑）。"""
        t = await models.get_task(db, task_id)
        if not t:
            await event.answer("任务不存在", alert=True)
            return

        emoji = "🔄" if t["type"] in ("sync", "sync_restricted") else "👁"
        status = STATUS_EMOJI.get(t["status"], "❓")

        # 解析源/目标完整信息（频道/群组 + 话题）。
        src_desc = await _format_chat_topic_display(
            t["source_chat_id"], t["source_topic_id"]
        )
        tgt_desc = await _format_chat_topic_display(
            t["target_chat_id"], t["target_topic_id"], prefer_bot=True
        )

        source_link = build_source_link(t["source_chat_id"],
                                         t["source_topic_id"])

        text = (
            f"{emoji} 任务 #{t['id']}\n"
            f"类型: {t['type']} | 模式: {t['mode']}\n"
            f"状态: {status} {t['status']}\n"
            f"源: {src_desc}\n"
            f"目标: {tgt_desc}")

        if t["type"] in models.CLONE_TASK_TYPES:
            done = int(t.get("done_count") or 0)
            total = int(t.get("total_count") or 0) or None
            # 已处理口径（成功+失败+跳过），处理完即为 100%
            processed = (done + int(t.get("fail_count") or 0)
                         + int(t.get("skip_count") or 0))
            text += (
                f"\n进度: {progress_bar(processed, total)}"
                f"\n游标: msg_id={int(t.get('last_synced_msg_id') or 0)}"
                f"\n成功 {done:,} · 失败 {int(t.get('fail_count') or 0):,}"
                f" · 跳过 {int(t.get('skip_count') or 0):,}")
            if t.get("last_error"):
                text += f"\n最近错误: {str(t['last_error'])[:80]}"
            follow_on = bool(int(t.get("follow") or 0))
            monitor_id = int(t.get("follow_task_id") or 0)
            text += ("\n持续监听: 🛰 开启"
                     + (f"（监听任务 #{monitor_id}）" if monitor_id else "")
                     if follow_on else "\n持续监听: ⏹ 关闭")
        else:
            # 监控任务：显示已转发条数、最近消息与最近活动
            try:
                stats = await monitor_manager.get_live_stats(task_id)
            except Exception:
                stats = {
                    "done": int(t.get("done_count") or 0),
                    "fail": int(t.get("fail_count") or 0),
                    "skip": int(t.get("skip_count") or 0),
                    "last_msg_id": int(t.get("last_synced_msg_id") or 0),
                    "last_error": t.get("last_error"),
                    "updated_at": t.get("updated_at"),
                }
            scope = (f"仅话题 #{t['source_topic_id']}"
                     if t.get("source_topic_id") else "全部新消息")
            text += (
                f"\n监听范围: {scope}"
                f"\n已转发 {stats['done']:,} 条 · 失败 {stats['fail']}"
                f" · 跳过 {stats['skip']}")
            if stats.get("last_msg_id"):
                text += f"\n最近消息: msg_id={stats['last_msg_id']}"
            if stats.get("updated_at"):
                text += f"\n最近活动: {to_local_time(stats['updated_at'])}"
            if stats.get("last_error"):
                text += f"\n最近错误: {str(stats['last_error'])[:80]}"

        buttons = []
        if source_link:
            buttons.append([Button.url("🔗 打开源", source_link)])

        if t["type"] in models.CLONE_TASK_TYPES:
            follow_on = bool(int(t.get("follow") or 0))
            buttons.append([Button.inline(
                "⏹ 关闭持续监听" if follow_on else "🛰 开启持续监听",
                data=f"follow:{task_id}")])

        action_row = []
        if t["status"] == "running":
            action_row.append(Button.inline("⏸ 暂停", data=f"pause:{task_id}"))
        elif t["status"] in ("paused", "failed"):
            action_row.append(Button.inline("▶️ 恢复", data=f"resume:{task_id}"))
        action_row.append(Button.inline("🗑 删除", data=f"delete:{task_id}"))
        buttons.append(action_row)

        buttons.append([Button.inline("« 返回列表", data="back_list")])
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"task:(\d+)"))
    async def on_task_detail(event):
        if not await _require_admin(event, alert=True):
            return
        task_id = int(event.pattern_match.group(1))
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"follow:(\d+)"))
    async def on_follow_toggle(event):
        if not await _require_admin(event, alert=True):
            return
        task_id = int(event.pattern_match.group(1))
        ok, msg = await _toggle_follow(task_id)
        await event.answer(msg, alert=not ok)
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"pause:(\d+)"))
    async def on_pause(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return

        if t["status"] == "running":
            await _stop_running_task(t)
        await models.update_task_status(db, task_id, "paused")
        logger.info("任务 #%s 已暂停", task_id)
        await event.answer(f"⏸ 任务 #{task_id} 已暂停")
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"resume:(\d+)"))
    async def on_resume(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return

        ok, err = await _resume_clone(task_id)
        if not ok:
            await event.answer(truncate(err or "无法继续", 120), alert=True)
        else:
            await event.answer(f"▶️ 任务 #{task_id} 已继续（从游标 "
                               f"{t.get('last_synced_msg_id') or 0} 之后）")
        await _show_task_detail(event, task_id)

    @bot.on(events.CallbackQuery(pattern=rb"delete:(\d+)"))
    async def on_delete(event):
        if not await _require_admin(event, alert=True):
            return

        task_id = int(event.pattern_match.group(1))
        t = await _get_task_or_alert(event, task_id)
        if not t:
            return

        if t["status"] == "running":
            await _stop_running_task(t)

        await models.update_task_status(db, task_id, "deleted")
        logger.info("任务 #%s 已删除", task_id)
        await event.answer(f"🗑 任务 #{task_id} 已删除")
        await _refresh_list(event)

    @bot.on(events.CallbackQuery(pattern=rb"clear_all"))
    async def on_clear_all(event):
        if not await _require_admin(event, alert=True):
            return

        tasks = await models.get_tasks_for_list(
            db, models.CLONE_TASK_TYPES + ("monitor",), limit=500)
        for t in tasks:
            if t["status"] == "running":
                await _stop_running_task(t)
            await models.update_task_status(db, t["id"], "deleted")

        logger.info("已清空所有任务，共 %d 个", len(tasks))
        await event.answer(f"🗑 已清空 {len(tasks)} 个任务")
        await event.edit("📭 所有任务已清空", buttons=None)

    @bot.on(events.CallbackQuery(pattern=rb"back_list"))
    async def on_back_list(event):
        if not await _require_admin(event, alert=True):
            return
        await _refresh_list(event)

    async def _refresh_list(event):
        """刷新任务列表内联键盘。"""
        tasks = await models.get_all_active_tasks(db)
        if not tasks:
            await event.edit("📭 没有活跃的任务", buttons=None)
            return
        buttons = await _build_task_buttons(tasks)
        await event.edit("📋 任务列表（点击管理）:", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"/settings(?:@\w+)?$"))
    async def on_settings(event):
        if not await _require_admin(event):
            return

        rl = _get_dynamic_rate_limit(config)
        await event.reply(
            "⚙️ 当前限流配置:\n"
            f"• batch_size: {rl.get('batch_size', 100)}\n"
            f"• forward_interval: {rl.get('forward_interval', [2, 5])}\n"
            f"• batch_pause_every: {rl.get('batch_pause_every', 50)}\n"
            f"• batch_pause_time: {rl.get('batch_pause_time', [30, 60])}\n"
            f"• flood_wait_multiplier: {rl.get('flood_wait_multiplier', 2)}\n"
            f"• max_flood_wait: {rl.get('max_flood_wait', 300)}")

    @bot.on(events.NewMessage(func=lambda e: e.is_private))
    async def on_private_link(event):
        """私聊发链接，自动解析并返回内容。"""
        if event.raw_text.startswith("/"):
            return
        if not allow_public and not is_admin(event.sender_id):
            return

        parsed_source = await _parse_private_link_and_source(event)
        if not parsed_source:
            return
        parsed, source_id = parsed_source

        fetch_target = await _resolve_private_fetch_target(parsed, source_id)
        if not fetch_target:
            await event.reply("❌ 无法获取频道的讨论群，评论消息无法解析")
            return

        # 多 UserBot 按顺序探测与鉴权
        active_ub = userbot
        if userbot_manager:
            active_ub, err_msg = await userbot_manager.resolve_accessible_userbot(
                fetch_target.chat_id, fetch_target.msg_id
            )
            if not active_ub:
                await event.reply(f"❌ {err_msg or '所有配置的 UserBot 均未加入该私有频道/群组，无法访问'}")
                return

        source_desc = await _describe_source_for_display(source_id, parsed, client=active_ub)
        comment_info = f" comment={parsed.comment_id}" if parsed.comment_id else ""
        single_info = " [single]" if parsed.single else ""
        phone = getattr(active_ub, "_phone", "default")
        logger.info("私聊解析: user=%s 源=%s msg=%s%s%s (UserBot: %s)",
                    event.sender_id, source_desc, parsed.msg_id,
                    comment_info, single_info, phone)

        # 预热 Bot 实体缓存，让策略1尽量命中
        await _pre_warm_bot_cache(parsed, source_id, fetch_target.chat_id)

        source_url = _build_source_url(event.raw_text, parsed, source_id)
        msg = await _fetch_private_target_message(event, fetch_target, client=active_ub)
        if not msg:
            return
        await _forward_private_message(event, fetch_target.chat_id, msg, parsed, client=active_ub, source_url=source_url)

    # main.py 通过该对象做启动时的任务恢复
    return HandlerRuntime(_auto_resume_clones, _sync_tasks)
