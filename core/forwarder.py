"""转发引擎：统一降级策略，按成本从低到高选择转发方式。

策略链（公共/私有频道同一套顺序，能力不足时逐级降级）：

1. **策略1 Bot 直接克隆** —— Bot 自己能读源、能在目标发言：原生转发（forward）
   或 `drop_author` 无痕转发（copy），必要时用 Bot 读到的媒体引用直接发送。
   全程服务端操作，不落地、不解码。
2. **策略2 UserBot 转发** —— Bot 读不到源（典型是「私有频道 + Bot 不在里面」）
   但 UserBot 在源里、且**在目标里有发言权**时，由 UserBot 直接 `forward_messages`。
   同样是一次服务端操作，不需要下载/上传；copy 模式用 `drop_author=True` 去掉来源标记。
3. **策略2b UserBot 读 + Bot 写** —— UserBot 能读但**不能往目标发**（例如只是目标
   频道管理员之外的身份）时，用 UserBot 取到的消息交给 Bot 发送，仍不下载。
4. **策略3 UserBot 下载 + Bot 上传** —— 源禁止转发（noforwards）、媒体引用失效等
   只能真正搬运字节时的兜底，最慢。

风控相关：来源/目标的能力判定会按 `transfer.probe_ttl_seconds` 缓存，避免每条消息
都去撞一次注定失败的权限错误；UserBot 路径有独立的、更保守的节奏与 FloodWait 熔断。
"""
import asyncio
import logging
import os
import random
import shutil
import tempfile
import time
from collections import Counter

from telethon import TelegramClient, errors, functions
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.types import Message
from telethon.tl.types import Channel, InputMessageID, MessageMediaDocument

from core.message_logic import (
    detect_hard_restriction,
    is_chat_globally_restricted,
    is_file_media,
    normalize_messages,
)
from core.media_transfer import MediaTransferHelper
from core.rate_limiter import RateLimiter

logger = logging.getLogger("tg_forward_bot.forwarder")

# 策略标识（统计 / 日志用）
ST_BOT = "bot"
ST_UB_FORWARD = "userbot_forward"
ST_UB_COPY = "userbot_copy"
ST_DOWNLOAD = "download"

STRATEGY_LABELS = {
    ST_BOT: "Bot直接",
    ST_UB_FORWARD: "UserBot转发",
    ST_UB_COPY: "UserBot读+Bot写",
    ST_DOWNLOAD: "下载+上传",
}


def _error_classes(*names: str) -> tuple:
    """按名字取 Telethon 异常类，缺失的自动忽略（兼容不同版本）。"""
    return tuple(c for c in (getattr(errors, n, None) for n in names) if c)


# 目标侧「写不进去」的典型错误：命中后本次运行不再重试 UserBot 直发
TARGET_WRITE_ERRORS = _error_classes(
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "ChannelPrivateError",
    "ChatRestrictedError", "ChatSendPlainForbiddenError", "ChatSendMediaForbiddenError",
)
# 源侧「读不到」的典型错误：命中后本次运行跳过策略1。
# 注意不要把 ChatAdminRequiredError 算进来：它既可能是"读源要管理员"，
# 也可能是"在目标发言要管理员"，误判会让策略1 被冤枉地停用 30 分钟。
SOURCE_READ_ERRORS = _error_classes(
    "ChannelPrivateError", "ChannelInvalidError",
)
# Bot 侧「写不进去」：只记日志，不做缓存（目标侧权限可能随时被调整）
BOT_WRITE_ERRORS = _error_classes(
    "ChatWriteForbiddenError", "ChatAdminRequiredError",
)
# 媒体引用过期/失效：必须重取消息刷新 file_reference 再试，否则这条媒体就永久丢了
FILE_REF_ERRORS = _error_classes(
    "FileReferenceExpiredError", "FileReferenceInvalidError",
)



class Forwarder:
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 config: dict, rate_limiter: RateLimiter):
        self.bot = bot
        self.userbot = userbot
        self.config = config
        self.rl = rate_limiter

        transfer_cfg = (config or {}).get("transfer", {})
        self.album_download_concurrency = max(
            1, int(transfer_cfg.get("album_download_concurrency", 3))
        )
        self.upload_part_size_kb = self._clamp_part_size_kb(
            transfer_cfg.get("upload_part_size_kb", 512)
        )
        self.download_part_size_kb = self._clamp_part_size_kb(
            transfer_cfg.get("download_part_size_kb", 512)
        )
        self.enable_fast_transfer = bool(transfer_cfg.get("enable_fast_transfer", True))
        self.fast_transfer_connections = int(transfer_cfg.get("fast_transfer_connections", 4))
        self.fast_download_connections = transfer_cfg.get("fast_download_connections")
        if self.fast_download_connections is not None:
            self.fast_download_connections = int(self.fast_download_connections)
        self.fast_upload_connections = transfer_cfg.get("fast_upload_connections")
        if self.fast_upload_connections is not None:
            self.fast_upload_connections = int(self.fast_upload_connections)
        self.fast_transfer_min_size_mb = int(transfer_cfg.get("fast_transfer_min_size_mb", 10))

        # ── 策略开关 ──────────────────────────────────────────────
        # UserBot 直接转发（策略2）：Bot 读不到源时最省事的一条路
        self.userbot_forward_enabled = bool(transfer_cfg.get("userbot_forward", True))
        # UserBot 读 + Bot 写（策略2b）：仍不下载，作为策略2 的降级
        self.userbot_copy_fallback = bool(transfer_cfg.get("userbot_copy_fallback", True))
        # 连续多少次 UserBot FloodWait 后本次运行放弃策略2（保护账号）
        self.userbot_max_floods = max(1, int(transfer_cfg.get("userbot_max_flood", 3)))
        # UserBot 遇 FloodWait 时在官方要求之上多等的秒数（别贴着红线跑）
        self.userbot_flood_buffer = max(0, int(transfer_cfg.get("userbot_flood_buffer", 5)))
        # 能力判定缓存有效期：避免每条消息都撞一次注定失败的权限错误
        self.probe_ttl_seconds = max(30, int(transfer_cfg.get("probe_ttl_seconds", 1800)))

        # ── 可选：目标是自己的频道时，让 UserBot 服务端转发送先 ──────────────
        # 默认关闭。原因：策略1（Bot 直接转发）本身就是服务端搬运，不占本机带宽，
        # 单条耗时约 0.3s；而 UserBot 受 3~6s 独立节奏限制，强行提前反而慢 10 倍。
        # 只有在「Bot 能在源里读、却在目标里发不出（被限制/无发言权）」这种
        # 特殊组合下，提前才有意义——那种场景 Bot 会先失败再降级，手动打开此开关
        # 可以省掉每条一次的失败探测。
        self.prefer_userbot_on_own_target = bool(
            transfer_cfg.get("prefer_userbot_on_own_target", False))

        # 单条策略超时：防「死连接/半开 TCP」把整个任务挂死。0 = 不限制
        self.strategy_timeout = max(0, int(transfer_cfg.get("strategy_timeout", 1200)))
        # 媒体引用过期后的重试次数（重取消息刷新 file_reference）
        self.file_ref_retries = max(0, int(transfer_cfg.get("file_ref_retries", 2)))
        # 下载预取深度：策略3 连续命中时，提前下载后续 1 条与上传重叠
        self.prefetch_ahead = max(0, int(transfer_cfg.get("prefetch_ahead", 1)))

        self._bot_unreadable: dict[int, float] = {}   # source -> 判定时间
        self._bot_readable: dict[int, float] = {}     # source -> 判定时间
        self._ub_write_blocked: dict[int, float] = {}  # target -> 判定时间
        self._noforwards: dict[int, float] = {}       # source -> 判定时间
        self._ub_floods = 0
        self._ub_forward_disabled = False
        self.strategy_counts: Counter[str] = Counter()
        self.strategy_timeouts = 0
        self.ref_retries = 0
        # 预取缓存：key=(source_chat_id, msg_id) -> {"path","tmpdir","task"}
        self._prefetch: dict[tuple[int, int], dict] = {}
        # 最近若干条命中策略3 的次数，用来决定「要不要开始预取」
        self._recent_download_hits = 0

        self.media = MediaTransferHelper(
            bot=self.bot,
            userbot=self.userbot,
            upload_part_size_kb=self.upload_part_size_kb,
            download_part_size_kb=self.download_part_size_kb,
            enable_fast_transfer=self.enable_fast_transfer,
            fast_transfer_connections=self.fast_transfer_connections,
            fast_transfer_min_size_mb=self.fast_transfer_min_size_mb,
            fast_download_connections=self.fast_download_connections,
            fast_upload_connections=self.fast_upload_connections,
        )

    @staticmethod
    def _select_best_alt_video(msg_or_media):
        """从文档对象的 alt_documents 中提取最高清晰度的 MP4 视频切片。

        排除 m3u8 播放列表与 storyboard；
        排序规则：分辨率高度 h 降序；同分辨率下优先 H.264 编码（全设备兼容性最佳）。
        返回: (best_doc, res_str, codec_str) 或 None
        """
        if not msg_or_media:
            return None
        media = getattr(msg_or_media, "media", msg_or_media)
        alt_docs = getattr(media, "alt_documents", None) or []
        if not alt_docs:
            return None

        candidates = []
        for alt in alt_docs:
            if getattr(alt, "mime_type", "") != "video/mp4":
                continue
            h = 0
            w = 0
            codec = ""
            for a in getattr(alt, "attributes", []):
                if hasattr(a, "h") and getattr(a, "h", 0):
                    h = a.h
                    w = getattr(a, "w", 0)
                if hasattr(a, "video_codec") and getattr(a, "video_codec", ""):
                    codec = a.video_codec.lower()
            if h > 0:
                is_h264 = 1 if ("h264" in codec or "avc" in codec) else 0
                candidates.append((h, is_h264, w, codec, alt))

        if not candidates:
            return None

        # 优先 h 降序，其次 is_h264 降序
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_h, best_is_h264, best_w, best_codec, best_alt = candidates[0]
        res_str = f"{best_w}x{best_h}" if best_w else f"{best_h}p"
        codec_str = best_codec or "未知编码"
        return best_alt, res_str, codec_str

    # ------------------------------------------------------------------
    # 能力判定缓存 / 指标
    # ------------------------------------------------------------------

    def _cache_valid(self, cache: dict[int, float], key: int) -> bool:
        ts = cache.get(key)
        if ts is None:
            return False
        if time.monotonic() - ts > self.probe_ttl_seconds:
            cache.pop(key, None)
            return False
        return True

    def _mark_bot_unreadable(self, source_chat_id: int, why: str = "") -> None:
        if not self._cache_valid(self._bot_unreadable, source_chat_id):
            logger.info("策略1: Bot 读不到源 chat=%s（%s）-> 本次运行跳过策略1，改用 UserBot 路径",
                        source_chat_id, why or "权限不足")
        self._bot_unreadable[source_chat_id] = time.monotonic()
        self._bot_readable.pop(source_chat_id, None)

    def _mark_bot_readable(self, source_chat_id: int) -> None:
        self._bot_readable[source_chat_id] = time.monotonic()
        self._bot_unreadable.pop(source_chat_id, None)

    def _mark_ub_blocked(self, target_chat_id: int, why: str = "") -> None:
        if not self._cache_valid(self._ub_write_blocked, target_chat_id):
            logger.info("策略2: UserBot 无法向目标 chat=%s 发言（%s）-> 本次运行改用「UserBot 读 + Bot 写」",
                        target_chat_id, why or "权限不足")
        self._ub_write_blocked[target_chat_id] = time.monotonic()

    def _mark_noforwards(self, source_chat_id: int) -> None:
        if not self._cache_valid(self._noforwards, source_chat_id):
            logger.info("源 chat=%s 禁止转发（noforwards）-> 全程走复制路径，不再尝试原生转发",
                        source_chat_id)
        self._noforwards[source_chat_id] = time.monotonic()

    def _userbot_forward_skip_reason(self, source_chat_id: int,
                                     target_chat_id: int,
                                     ub: TelegramClient | None,
                                     caller: str = "monitor") -> str | None:
        """策略2 的前置判定，返回跳过原因（None 表示可以尝试）。"""
        if not self.userbot_forward_enabled:
            return "transfer.userbot_forward=false"
        if not ub:
            return "无可用 UserBot"
        if caller == "private" or target_chat_id > 0:
            # 私聊场景保留原来的 Bot 发送与 >300MB 切片规则，不改变既有观感
            return "私聊目标不走 UserBot 转发"
        if self._ub_forward_disabled:
            return f"本次运行 UserBot FloodWait 已超 {self.userbot_max_floods} 次，暂停直发"
        if self._cache_valid(self._noforwards, source_chat_id):
            return "源禁止转发(noforwards)"
        if self._cache_valid(self._ub_write_blocked, target_chat_id):
            return "该目标已被判定 UserBot 无发言权"
        return None

    @staticmethod
    def _looks_like_unreadable(error: Exception) -> bool:
        """异常是否意味着「这个客户端压根读不到该源」。"""
        if isinstance(error, SOURCE_READ_ERRORS):
            return True
        if isinstance(error, ValueError):
            text = str(error).lower()
            return "entity" in text or "peer" in text
        return False

    def _should_prefer_userbot(self, source_chat_id: int, target_chat_id: int,
                               userbot: TelegramClient | None,
                               caller: str = "syncer") -> bool:
        """目标是自己频道时，是否让 UserBot 服务端转发送先。

        只做「顺序调整」不改链路：一旦 UserBot 那条路不通（无发言权/禁止转发），
        策略2 会自己返回 None，循环继续走策略1（Bot 直接），行为仍然收敛。
        """
        if not self.prefer_userbot_on_own_target:
            return False
        if target_chat_id >= 0:
            # 私聊 / 普通群：既有 Bot 切片规则更稳，保持原顺序
            return False
        if caller == "private":
            return False
        return self._userbot_forward_skip_reason(
            source_chat_id, target_chat_id, userbot or self.userbot, caller) is None

    def should_prefetch(self) -> bool:
        """最近一段时间策略3 占主导时，才值得提前下载（否则预取全是白耗带宽）。"""
        return self.prefetch_ahead > 0 and self._recent_download_hits >= 3

    def _note_strategy_hit(self, key: str) -> None:
        """记录最近策略命中，用于判断是否进入「下载型」工作模式。"""
        if key == ST_DOWNLOAD:
            self._recent_download_hits = min(10, self._recent_download_hits + 1)
        elif key in (ST_BOT, ST_UB_FORWARD):
            self._recent_download_hits = max(0, self._recent_download_hits - 1)

    def strategy_summary(self) -> str:
        """本次运行各策略命中次数（用于任务完成消息）。"""
        parts = [
            f"{STRATEGY_LABELS[key]} {self.strategy_counts[key]}"
            for key in (ST_BOT, ST_UB_FORWARD, ST_UB_COPY, ST_DOWNLOAD)
            if self.strategy_counts.get(key)
        ]
        return " · ".join(parts)

    def diagnostics_summary(self) -> str:
        """自愈类事件计数：超时止损 / 媒体引用刷新。"""
        parts = []
        if self.strategy_timeouts:
            parts.append(f"超时止损 {self.strategy_timeouts} 次")
        if self.ref_retries:
            parts.append(f"引用刷新 {self.ref_retries} 次")
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # 原生转发（Bot / UserBot 共用）
    # ------------------------------------------------------------------

    async def _native_forward(self, client: TelegramClient, target_chat_id: int,
                              msg_ids: int | list[int], source_ref,
                              topic_id: int | None = None,
                              drop_author: bool = True):
        """原生转发（服务端搬运，不下载不重传）。

        若带 topic_id 且目标是超级群，注入 top_msg_id 确保落在指定论坛话题。
        `drop_author=True` 用于 copy 模式：去掉「转发自 X」标记。
        """
        is_single = isinstance(msg_ids, int)
        ids_list = [msg_ids] if is_single else list(msg_ids)
        if not ids_list:
            return None

        payload = ids_list if not is_single else ids_list[0]

        # 无 topic_id 或目标是私聊，直接用 Telethon 官方 forward_messages
        if not topic_id or target_chat_id > 0:
            return await client.forward_messages(
                target_chat_id, payload, source_ref, drop_author=drop_author)

        # 带有 topic_id 的超级群：使用 MTProto 底层 ForwardMessagesRequest 携带 top_msg_id
        try:
            to_peer = await client.get_input_entity(target_chat_id)
            from_peer = await client.get_input_entity(source_ref)
            random_ids = [random.randint(-2**63, 2**63 - 1) for _ in ids_list]
            req = functions.messages.ForwardMessagesRequest(
                from_peer=from_peer,
                id=ids_list,
                to_peer=to_peer,
                drop_author=drop_author,
                top_msg_id=topic_id,
                random_id=random_ids
            )
            updates = await client(req)
            sent_msgs = []
            for u in getattr(updates, "updates", []):
                m = getattr(u, "message", None)
                if m:
                    sent_msgs.append(m)
            if is_single:
                return sent_msgs[0] if sent_msgs else None
            return sent_msgs
        except Exception as e:
            logger.warning("带 topic_id 底层原生转发异常(%s: %s)，回退普通 forward_messages",
                           type(e).__name__, e)
            return await client.forward_messages(
                target_chat_id, payload, source_ref, drop_author=drop_author)

    async def _forward_messages_drop_author(
        self, target_chat_id: int, msg_ids: int | list[int],
        source_ref, topic_id: int | None = None
    ):
        """兼容旧调用：Bot 侧无痕转发。"""
        return await self._native_forward(
            self.bot, target_chat_id, msg_ids, source_ref,
            topic_id=topic_id, drop_author=True)


    async def forward_message(self, source_chat_id: int, msg_id: int,
                              target_chat_id: int, mode: str = "copy",
                              target_topic_id: int | None = None,
                              caller: str = "monitor",
                              userbot: TelegramClient | None = None,
                              with_fail_marker: bool = True) -> int | None:
        """
        转发单条消息，返回目标消息 ID。失败返回 None。
        降级链：Bot直接 → UserBot转发 → UserBot读+Bot写 → UserBot下载+Bot上传 → 发失败标记

        with_fail_marker=False 时不发 #fail2forward 标记：评论这类"附带内容"
        失败一条不该往目标频道灌一条占位消息，也避免调用方把标记的 ID 误判成成功。
        """
        result = await self._run_message_strategies(
            source_chat_id, msg_id, target_chat_id, mode, target_topic_id,
            caller=caller, userbot=userbot
        )
        if result is not None:
            return result

        # 策略4: 发送失败标记（私聊环境/评论等附带内容跳过）
        is_private = (caller == "private") or (target_chat_id > 0)
        if is_private:
            logger.warning("msg=%s 所有策略失败，私聊环境跳过发送 #fail2forward 标记", msg_id)
            return None
        if not with_fail_marker:
            logger.warning("msg=%s 所有策略失败，按调用方要求跳过 #fail2forward 标记", msg_id)
            return None

        logger.warning("msg=%s 所有策略失败，发送 #fail2forward 标记", msg_id)
        return await self.send_fail_marker(
            source_chat_id, msg_id, target_chat_id, target_topic_id, userbot=userbot
        )

    async def detect_restriction(
        self,
        source_chat_id: int,
        msg_id: int | None = None,
        userbot: TelegramClient | None = None,
    ) -> tuple[bool, str]:
        """硬封禁并集：message.restriction_reason.platform=all 或 chat级全平台封禁。"""
        ub = userbot or self.userbot
        chat_globally_restricted = False
        try:
            entity = await ub.get_entity(source_chat_id)
            chat_globally_restricted = is_chat_globally_restricted(entity)
        except Exception:
            chat_globally_restricted = False

        restricted, field = detect_hard_restriction(chat_globally_restricted, None)
        if restricted:
            return restricted, field

        if msg_id is None:
            return False, ""

        try:
            msg = await self._get_single_message(ub, source_chat_id, msg_id)
            return detect_hard_restriction(chat_globally_restricted, msg)
        except Exception:
            pass

        return False, ""

    async def forward_album(self, source_chat_id: int, msg_ids: list[int],
                            target_chat_id: int, mode: str = "copy",
                            target_topic_id: int | None = None,
                            caller: str = "monitor",
                            userbot: TelegramClient | None = None) -> list[int]:
        """
        转发相册（grouped media），尽量保持为同一组发送。
        失败时降级为逐条转发。
        """
        if not msg_ids:
            return []

        msg_ids = sorted(set(msg_ids))
        result = await self._run_album_strategies(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id,
            caller=caller, userbot=userbot
        )
        if result:
            return result

        forwarded: list[int] = []
        for mid in msg_ids:
            target_mid = await self.forward_message(
                source_chat_id, mid, target_chat_id, mode, target_topic_id,
                caller=caller, userbot=userbot)
            if target_mid:
                forwarded.append(target_mid)
        return forwarded

    async def _run_message_strategies(self, source_chat_id: int, msg_id: int,
                                      target_chat_id: int, mode: str,
                                      target_topic_id: int | None,
                                      caller: str = "monitor",
                                      userbot: TelegramClient | None = None) -> int | None:
        # 等待（节奏控制）放在各策略内部：被跳过的层级不白等，Bot/UserBot 各按自己的节奏
        strategies = [
            (ST_BOT, "策略1(Bot直接)", self._try_bot_direct,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id, caller)),
            (ST_UB_FORWARD, "策略2(UserBot转发)", self._try_userbot_forward,
             (source_chat_id, msg_id, target_chat_id, mode, target_topic_id, caller, userbot)),
            (ST_UB_COPY, "策略2b(UserBot读+Bot写)", self._try_userbot_copy_via_bot,
             (source_chat_id, msg_id, target_chat_id, target_topic_id, caller, userbot)),
            (ST_DOWNLOAD, "策略3(下载+上传)", self._try_userbot_download_bot_upload,
             (source_chat_id, msg_id, target_chat_id, target_topic_id, userbot)),
        ]
        if self._should_prefer_userbot(source_chat_id, target_chat_id, userbot, caller):
            strategies[0], strategies[1] = strategies[1], strategies[0]
        return await self._run_strategies(
            strategies, f"msg={msg_id}", is_album=False)

    async def _run_album_strategies(self, source_chat_id: int, msg_ids: list[int],
                                    target_chat_id: int, mode: str,
                                    target_topic_id: int | None,
                                    caller: str = "monitor",
                                    userbot: TelegramClient | None = None) -> list[int]:
        strategies = [
            (ST_BOT, "策略1(Bot直接)", self._try_bot_direct_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id, caller)),
            (ST_UB_FORWARD, "策略2(UserBot转发)", self._try_userbot_forward_album,
             (source_chat_id, msg_ids, target_chat_id, mode, target_topic_id, caller, userbot)),
            (ST_UB_COPY, "策略2b(UserBot读+Bot写)", self._try_userbot_copy_via_bot_album,
             (source_chat_id, msg_ids, target_chat_id, target_topic_id, caller, userbot)),
            (ST_DOWNLOAD, "策略3(下载+上传)", self._try_userbot_download_bot_upload_album,
             (source_chat_id, msg_ids, target_chat_id, target_topic_id, userbot)),
        ]
        if self._should_prefer_userbot(source_chat_id, target_chat_id, userbot, caller):
            strategies[0], strategies[1] = strategies[1], strategies[0]
        return await self._run_strategies(
            strategies, f"album={msg_ids}", is_album=True)

    async def _run_strategies(self, strategies: list, label: str,
                              *, is_album: bool):
        """按顺序尝试各策略，单条超时后继续降级。

        超时的意义不在提速，而在「止损」：半开连接/TCP 假死时 Telethon 的请求
        可以永久挂住，一个挂住的请求会让整个克隆任务停在同一游标上不动。
        """
        for key, strategy_name, strategy_func, args in strategies:
            try:
                result = await self._call_with_timeout(
                    strategy_func, args, f"{strategy_name} {label}")
            except asyncio.TimeoutError:
                self.strategy_timeouts += 1
                logger.warning("%s %s 超时 %ds，跳过该策略继续降级",
                               label, strategy_name, self.strategy_timeout)
                continue
            if result:
                self.strategy_counts[key] += 1
                self._note_strategy_hit(key)
                logger.info("%s %s成功 -> %s", label, strategy_name,
                            result if is_album else result)
                return result
        return [] if is_album else None

    async def _call_with_timeout(self, func, args: tuple, label: str):
        """按 transfer.strategy_timeout 限制单次策略耗时（0 表示不限制）。"""
        if self.strategy_timeout <= 0:
            return await func(*args)
        return await asyncio.wait_for(func(*args), timeout=self.strategy_timeout)


    async def _try_bot_direct(self, source_chat_id, msg_id,
                              target_chat_id, mode, topic_id,
                              caller: str = "monitor") -> int | None:
        is_private = (caller == "private") or (target_chat_id > 0)
        if self._cache_valid(self._bot_unreadable, source_chat_id):
            logger.debug("策略1: msg=%s 已在本次运行判定 Bot 读不到源，跳过", msg_id)
            return None
        await self.rl.wait()
        logger.info("策略1: msg=%s 收到调用 caller=%s is_private=%s chat=%s mode=%s",
                    msg_id, caller, is_private, source_chat_id, mode)
        try:
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_id, source_ref)
                if result:
                    self._mark_bot_readable(source_chat_id)
                return result.id if result else None

            # mode == "copy":
            # 1. 优先尝试未受限频道原生无痕转发 (drop_author=True)，所有 caller 均生效 [保留全套切片与 HLS]
            can_drop_author = True
            unreadable_signal = None
            try:
                source_entity = await self.bot.get_entity(source_ref)
                if getattr(source_entity, "noforwards", False):
                    can_drop_author = False
                    self._mark_noforwards(source_chat_id)
            except Exception:
                pass

            if can_drop_author:
                try:
                    logger.info("策略1: msg=%s 命中未受限频道，执行无痕原生转发(drop_author=True caller=%s) [保留原生全套画质切片]",
                                msg_id, caller)
                    result = await self._forward_messages_drop_author(
                        target_chat_id, msg_id, source_ref, topic_id=topic_id)
                    res_id = getattr(result, "id", None)
                    if res_id:
                        logger.info("策略1: msg=%s 无痕原生转发成功 -> target_msg=%s (全套画质切片保留 caller=%s)",
                                    msg_id, res_id, caller)
                        return res_id
                except errors.ChatForwardsRestrictedError:
                    logger.info("策略1: msg=%s 转发受限(ChatForwardsRestrictedError)，转入受限处理流程", msg_id)
                except Exception as e:
                    if self._looks_like_unreadable(e):
                        unreadable_signal = e
                    logger.info("策略1: msg=%s 无痕转发异常(%s: %s)，转入受限处理流程", msg_id, type(e).__name__, e)

            # 2. 受限或无痕转发失败：获取源消息
            msg = await self._get_single_message_for_bot(source_chat_id, source_ref, msg_id)
            if not msg:
                if unreadable_signal is not None:
                    self._mark_bot_unreadable(source_chat_id, type(unreadable_signal).__name__)
                logger.info("策略1: msg=%s Bot 无法获取消息", msg_id)
                return None

            # 私聊特化逻辑：>300MB 视频切片提取（带体积反超熔断保护）
            if is_private:
                doc = getattr(getattr(msg, "media", None), "document", None)
                sz_bytes = getattr(doc, "size", 0)
                sz_mb = sz_bytes / 1024 / 1024
                if doc and sz_bytes > 300 * 1024 * 1024:
                    alt_res = self._select_best_alt_video(msg)
                    if alt_res:
                        best_alt, res_str, codec_str = alt_res
                        alt_sz_bytes = getattr(best_alt, "size", 0)
                        alt_sz_mb = alt_sz_bytes / 1024 / 1024
                        # 检查切片体积是否反超母文件 (需求 2)
                        if alt_sz_bytes > sz_bytes:
                            logger.info(
                                "策略1[私聊]: msg=%s 切片大小(%.1fMB) > 母文件大小(%.1fMB) -> 【放弃切片: 体积反超母文件】，回退发送原母文件",
                                msg_id, alt_sz_mb, sz_mb
                            )
                        else:
                            logger.info(
                                "策略1[私聊]: msg=%s 命中>300MB大文件规则 (母文件=%.1fMB) -> 【已选择切片】(清晰度=%s, 编码=%s, 切片大小=%.1fMB, id=%s)",
                                msg_id, sz_mb, res_str, codec_str, alt_sz_mb, best_alt.id
                            )
                            result = await self.bot.send_file(
                                target_chat_id, best_alt,
                                caption=msg.text or "",
                                supports_streaming=True,
                                **self._reply_kwargs(topic_id)
                            )
                            return result.id if result else None
                    else:
                        logger.info(
                            "策略1[私聊]: msg=%s 文件大小=%.1fMB > 300MB -> 【未选择切片: 无可用MP4切片】，回退常规复制母文件",
                            msg_id, sz_mb
                        )
                elif doc:
                    logger.info(
                        "策略1[私聊]: msg=%s 文件大小=%.1fMB <= 300MB -> 【未选择切片: 体积未超限】，走常规复制母文件",
                        msg_id, sz_mb
                    )

            # 常规复制（非私聊或切片放弃/回退）
            result = await self._copy_message(self.bot, msg, target_chat_id, topic_id)
            if result:
                self._mark_bot_readable(source_chat_id)
                return result.id
            return None
        except errors.ChatForwardsRestrictedError as e:
            logger.info("策略1: msg=%s Bot 转发受限: %s", msg_id, type(e).__name__)
            self._mark_noforwards(source_chat_id)
            return None
        except SOURCE_READ_ERRORS as e:
            logger.info("策略1: msg=%s Bot 读不到源: %s", msg_id, type(e).__name__)
            self._mark_bot_unreadable(source_chat_id, type(e).__name__)
            return None
        except BOT_WRITE_ERRORS as e:
            logger.info("策略1: msg=%s Bot 无发言权限: %s", msg_id, type(e).__name__)
            return None
        except ValueError as e:
            if "entity" in str(e).lower() or "peer" in str(e).lower():
                logger.info("策略1: msg=%s Bot 无法解析源实体: %s", msg_id, e)
                self._mark_bot_unreadable(source_chat_id, "无法解析实体")
                return None
            logger.warning("策略1: msg=%s Bot 异常: %s", msg_id, e)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_bot_direct,
                                            source_chat_id, msg_id,
                                            target_chat_id, mode, topic_id, caller)
        except Exception as e:
            logger.warning("策略1: msg=%s Bot 异常: %s", msg_id, e)
            return None

    async def _try_bot_direct_album(self, source_chat_id, msg_ids,
                                    target_chat_id, mode, topic_id,
                                    caller: str = "monitor") -> list[int]:
        is_private = (caller == "private") or (target_chat_id > 0)
        if self._cache_valid(self._bot_unreadable, source_chat_id):
            logger.debug("策略1相册: msgs=%s 已在本次运行判定 Bot 读不到源，跳过", msg_ids)
            return []
        await self.rl.wait()
        logger.info("策略1相册: msgs=%s 收到调用 caller=%s is_private=%s chat=%s mode=%s",
                    msg_ids, caller, is_private, source_chat_id, mode)
        try:
            source_ref = await self._resolve_source_for_bot(source_chat_id)
            if mode == "forward":
                result = await self.bot.forward_messages(
                    target_chat_id, msg_ids, source_ref)
                ids = self._extract_result_ids(result)
                if ids:
                    self._mark_bot_readable(source_chat_id)
                return ids

            # mode == "copy":
            # 1. 优先尝试未受限频道原生无痕相册转发 (drop_author=True)，所有 caller 均生效
            can_drop_author = True
            unreadable_signal = None
            try:
                source_entity = await self.bot.get_entity(source_ref)
                if getattr(source_entity, "noforwards", False):
                    can_drop_author = False
                    self._mark_noforwards(source_chat_id)
            except Exception:
                pass

            if can_drop_author:
                try:
                    logger.info("策略1相册: msgs=%s 命中未受限频道，执行无痕原生相册转发(drop_author=True caller=%s)",
                                msg_ids, caller)
                    result = await self._forward_messages_drop_author(
                        target_chat_id, msg_ids, source_ref, topic_id=topic_id)
                    res_ids = self._extract_result_ids(result)
                    if res_ids:
                        logger.info("策略1相册: msgs=%s 无痕原生相册转发成功 -> target_msgs=%s (caller=%s)",
                                    msg_ids, res_ids, caller)
                        return res_ids
                except errors.ChatForwardsRestrictedError:
                    logger.info("策略1相册: msgs=%s 转发受限，转入受限处理流程", msg_ids)
                except Exception as e:
                    if self._looks_like_unreadable(e):
                        unreadable_signal = e
                    logger.info("策略1相册: msgs=%s 无痕相册转发异常(%s: %s)，转入受限处理流程",
                                msg_ids, type(e).__name__, e)

            # 2. 受限或无痕转发失败：获取消息列表
            msgs = await self._get_message_list_for_bot(source_chat_id, source_ref, msg_ids)
            if not msgs:
                if unreadable_signal is not None:
                    self._mark_bot_unreadable(source_chat_id, type(unreadable_signal).__name__)
                logger.info("策略1相册: Bot 无法获取消息 %s", msg_ids)
                return []

            if is_private:
                for m in msgs:
                    doc = getattr(getattr(m, "media", None), "document", None)
                    sz_bytes = getattr(doc, "size", 0)
                    sz_mb = sz_bytes / 1024 / 1024
                    if doc and sz_bytes > 300 * 1024 * 1024:
                        alt_res = self._select_best_alt_video(m)
                        if alt_res:
                            best_alt, res_str, codec_str = alt_res
                            alt_sz_bytes = getattr(best_alt, "size", 0)
                            alt_sz_mb = alt_sz_bytes / 1024 / 1024
                            if alt_sz_bytes > sz_bytes:
                                logger.info(
                                    "策略1相册[私聊]: 子消息 msg=%s 切片大小(%.1fMB) > 母文件大小(%.1fMB) -> 【放弃切片: 体积反超母文件】，保留原母文件",
                                    m.id, alt_sz_mb, sz_mb
                                )
                            else:
                                logger.info(
                                    "策略1相册[私聊]: 子消息 msg=%s (%.1fMB) 命中>300MB规则 -> 【已选择切片】(%s, %s, %.1fMB, id=%s)",
                                    m.id, sz_mb, res_str, codec_str, alt_sz_mb, best_alt.id
                                )
                                m.media = MessageMediaDocument(document=best_alt)
                        else:
                            logger.info("策略1相册[私聊]: 子消息 msg=%s (%.1fMB) -> 【未选择切片: 无可用MP4切片】",
                                        m.id, sz_mb)
                    elif doc:
                        logger.info("策略1相册[私聊]: 子消息 msg=%s (%.1fMB) -> 【未选择切片: 体积未超限】",
                                    m.id, sz_mb)

            result = await self._copy_album(self.bot, msgs, target_chat_id, topic_id)
            ids = self._extract_result_ids(result)
            if ids:
                self._mark_bot_readable(source_chat_id)
            return ids
        except errors.ChatForwardsRestrictedError as e:
            logger.info("策略1相册: Bot 转发受限: %s", type(e).__name__)
            self._mark_noforwards(source_chat_id)
            return []
        except SOURCE_READ_ERRORS as e:
            logger.info("策略1相册: Bot 读不到源: %s", type(e).__name__)
            self._mark_bot_unreadable(source_chat_id, type(e).__name__)
            return []
        except BOT_WRITE_ERRORS as e:
            logger.info("策略1相册: Bot 无发言权限: %s", type(e).__name__)
            return []
        except ValueError as e:
            if "entity" in str(e).lower() or "peer" in str(e).lower():
                logger.info("策略1相册: Bot 无法解析源实体: %s", e)
                self._mark_bot_unreadable(source_chat_id, "无法解析实体")
                return []
            logger.warning("策略1相册: Bot 异常: %s", e)
            return []
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_bot_direct_album,
                source_chat_id, msg_ids, target_chat_id, mode, topic_id, caller)
        except Exception as e:
            logger.warning("策略1相册: Bot 异常: %s", e)
            return []

    # ------------------------------------------------------------------
    # 策略2：UserBot 直接转发（服务端搬运，不下载不重传）
    # ------------------------------------------------------------------

    async def _try_userbot_forward(self, source_chat_id, msg_id,
                                   target_chat_id, mode, topic_id,
                                   caller: str = "monitor",
                                   userbot: TelegramClient | None = None) -> int | None:
        ub = userbot or self.userbot
        skip = self._userbot_forward_skip_reason(source_chat_id, target_chat_id, ub, caller)
        if skip:
            logger.info("策略2: msg=%s 跳过（%s）", msg_id, skip)
            return None

        # UserBot 是用户账号，节奏比 Bot 更保守
        await self.rl.wait(kind="userbot")
        try:
            logger.info("策略2: msg=%s 由 UserBot 直接转发 drop_author=%s",
                        msg_id, mode != "forward")
            result = await self._native_forward(
                ub, target_chat_id, msg_id, source_chat_id,
                topic_id=topic_id, drop_author=(mode != "forward"))
            if result:
                self._ub_floods = 0
                return result.id if not isinstance(result, list) else (
                    result[0].id if result and getattr(result[0], "id", None) else None)
            return None
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2: msg=%s 源禁止转发(ChatForwardsRestrictedError)", msg_id)
            self._mark_noforwards(source_chat_id)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_userbot_flood(
                e, self._try_userbot_forward,
                source_chat_id, msg_id, target_chat_id, mode, topic_id, caller, ub)
        except TARGET_WRITE_ERRORS as e:
            logger.info("策略2: msg=%s UserBot 无法写入目标: %s", msg_id, type(e).__name__)
            self._mark_ub_blocked(target_chat_id, type(e).__name__)
            return None
        except Exception as e:
            logger.warning("策略2: msg=%s 异常: %s", msg_id, e)
            return None

    async def _try_userbot_forward_album(self, source_chat_id, msg_ids,
                                         target_chat_id, mode, topic_id,
                                         caller: str = "monitor",
                                         userbot: TelegramClient | None = None) -> list[int]:
        ub = userbot or self.userbot
        skip = self._userbot_forward_skip_reason(source_chat_id, target_chat_id, ub, caller)
        if skip:
            logger.info("策略2相册: msgs=%s 跳过（%s）", msg_ids, skip)
            return []

        await self.rl.wait(kind="userbot")
        try:
            logger.info("策略2相册: msgs=%s 由 UserBot 直接整组转发 drop_author=%s",
                        msg_ids, mode != "forward")
            result = await self._native_forward(
                ub, target_chat_id, msg_ids, source_chat_id,
                topic_id=topic_id, drop_author=(mode != "forward"))
            ids = self._extract_result_ids(result)
            if ids:
                self._ub_floods = 0
            return ids
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2相册: msgs=%s 源禁止转发", msg_ids)
            self._mark_noforwards(source_chat_id)
            return []
        except errors.FloodWaitError as e:
            return await self._handle_userbot_flood(
                e, self._try_userbot_forward_album,
                source_chat_id, msg_ids, target_chat_id, mode, topic_id, caller, ub)
        except TARGET_WRITE_ERRORS as e:
            logger.info("策略2相册: msgs=%s UserBot 无法写入目标: %s",
                        msg_ids, type(e).__name__)
            self._mark_ub_blocked(target_chat_id, type(e).__name__)
            return []
        except Exception as e:
            logger.warning("策略2相册: msgs=%s 异常: %s", msg_ids, e)
            return []

    # ------------------------------------------------------------------
    # 策略2b：UserBot 读 + Bot 写（仍不下载，只是换了发送方）
    # ------------------------------------------------------------------

    async def _try_userbot_copy_via_bot(self, source_chat_id, msg_id,
                                        target_chat_id, topic_id,
                                        caller: str = "monitor",
                                        userbot: TelegramClient | None = None) -> int | None:
        if not self.userbot_copy_fallback:
            return None
        ub = userbot or self.userbot
        await self.rl.wait()
        try:
            msg = await self._get_single_message(ub, source_chat_id, msg_id)
            if not msg:
                logger.info("策略2b: msg=%s UserBot 无法获取消息", msg_id)
                return None
            result = await self._with_fresh_ref(
                ub, source_chat_id, msg_id,
                lambda m: self._copy_message(self.bot, m, target_chat_id, topic_id),
                label="策略2b", initial=msg)
            return result.id if result else None
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2b: msg=%s 转发受限", msg_id)
            self._mark_noforwards(source_chat_id)
            return None
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_userbot_copy_via_bot,
                                            source_chat_id, msg_id,
                                            target_chat_id, topic_id, caller, ub)
        except Exception as e:
            logger.warning("策略2b: msg=%s 异常: %s", msg_id, e)
            return None

    async def _try_userbot_copy_via_bot_album(self, source_chat_id, msg_ids,
                                              target_chat_id, topic_id,
                                              caller: str = "monitor",
                                              userbot: TelegramClient | None = None) -> list[int]:
        if not self.userbot_copy_fallback:
            return []
        ub = userbot or self.userbot
        await self.rl.wait()
        try:
            msgs = await self._get_message_list(ub, source_chat_id, msg_ids)
            if not msgs:
                logger.info("策略2b相册: UserBot 无法获取消息 %s", msg_ids)
                return []
            result = await self._with_fresh_ref_list(
                ub, source_chat_id, msg_ids,
                lambda ms: self._copy_album(self.bot, ms, target_chat_id, topic_id),
                label="策略2b相册", initial=msgs)
            return self._extract_result_ids(result)
        except errors.ChatForwardsRestrictedError:
            logger.info("策略2b相册: 转发受限")
            self._mark_noforwards(source_chat_id)
            return []
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_copy_via_bot_album,
                source_chat_id, msg_ids, target_chat_id, topic_id, caller, ub)
        except Exception as e:
            logger.warning("策略2b相册: 异常: %s", e)
            return []

    async def _try_userbot_download_bot_upload(self, source_chat_id, msg_id,
                                               target_chat_id, topic_id,
                                               userbot: TelegramClient | None = None) -> int | None:
        ub = userbot or self.userbot
        await self.rl.wait()
        try:
            msg = await self._get_single_message(ub, source_chat_id, msg_id)
            if not msg:
                logger.info("策略3: msg=%s UserBot 无法获取消息", msg_id)
                return None
            return await self._with_fresh_ref(
                ub, source_chat_id, msg_id,
                lambda m: self._download_and_upload(
                    ub, m, source_chat_id, target_chat_id, topic_id),
                label="策略3", initial=msg)
        except errors.FloodWaitError as e:
            return await self._handle_flood(e, self._try_userbot_download_bot_upload,
                                            source_chat_id, msg_id,
                                            target_chat_id, topic_id, ub)
        except Exception as e:
            logger.warning("策略3: msg=%s 异常: %s", msg_id, e)
            return None

    async def _download_and_upload(self, ub: TelegramClient, msg: Message,
                                   source_chat_id: int, target_chat_id: int,
                                   topic_id: int | None) -> int | None:
        """单条媒体：优先用预取结果（下载已与上一条的上传重叠），否则现下现传。"""
        reply_to = self._reply_to(topic_id)
        msg_id = msg.id

        if is_file_media(msg):
            prefetched = await self._take_prefetch(source_chat_id, msg_id)
            if prefetched:
                path, pf_tmpdir = prefetched
                logger.info("策略3: msg=%s 命中预取缓存，跳过下载", msg_id)
                try:
                    thumb_path = await self.media.download_video_thumb_to_path(
                        msg, pf_tmpdir, userbot=ub)
                    send_kwargs = self.media.build_send_file_kwargs(
                        msg, reply_to, thumb_path=thumb_path)
                    result = await self.media.send_file_with_compat(
                        target_chat_id, path, **send_kwargs)
                    return result.id if result else None
                finally:
                    shutil.rmtree(pf_tmpdir, ignore_errors=True)

            with tempfile.TemporaryDirectory() as tmpdir:
                path = await self.media.download_media_to_path(msg, tmpdir, userbot=ub)
                if not path:
                    logger.warning("策略3: msg=%s 媒体下载失败", msg_id)
                    return None
                thumb_path = await self.media.download_video_thumb_to_path(
                    msg, tmpdir, userbot=ub)
                send_kwargs = self.media.build_send_file_kwargs(
                    msg, reply_to, thumb_path=thumb_path)
                result = await self.media.send_file_with_compat(
                    target_chat_id, path, **send_kwargs)
                return result.id if result else None

        if msg.text:
            result = await self.bot.send_message(
                target_chat_id, msg.text, reply_to=reply_to)
            return result.id if result else None

        logger.info("策略3: msg=%s 消息无文本也无媒体", msg_id)
        return None

    # ------------------------------------------------------------------
    # 下载预取（让「下载第 N+1 条」与「上传第 N 条」重叠）
    # ------------------------------------------------------------------

    async def prefetch_media(self, source_chat_id: int, msg_id: int,
                             userbot: TelegramClient | None = None) -> None:
        """后台提前下载一条媒体，供紧接着的策略3 直接取用。

        策略3 原本是严格串行（取消息 → 下载 → 上传），单条大文件里上传占一半时间，
        这段时间 UserBot 完全空闲。预取把两条消息的下载/上传叠在一起，
        媒体型频道的实际耗时约减半。
        """
        if not self.should_prefetch():
            return
        key = (source_chat_id, msg_id)
        if key in self._prefetch:
            return
        if len(self._prefetch) >= max(1, self.prefetch_ahead):
            return
        entry: dict = {"task": None, "path": None, "tmpdir": None}
        self._prefetch[key] = entry
        entry["task"] = asyncio.create_task(
            self._prefetch_worker(key, source_chat_id, msg_id, userbot))

    async def _prefetch_worker(self, key, source_chat_id: int, msg_id: int,
                               userbot: TelegramClient | None) -> None:
        ub = userbot or self.userbot
        tmpdir = tempfile.mkdtemp(prefix="tg_prefetch_")
        entry = self._prefetch.get(key)
        try:
            msg = await self._get_single_message(ub, source_chat_id, msg_id)
            if not msg or not is_file_media(msg):
                self._drop_prefetch(key, tmpdir)
                return
            path = await self.media.download_media_to_path(msg, tmpdir, userbot=ub)
            if not path or entry is None:
                self._drop_prefetch(key, tmpdir)
                return
            entry["path"] = path
            entry["tmpdir"] = tmpdir
            logger.debug("预取完成 msg=%s -> %s", msg_id, os.path.basename(path))
        except asyncio.CancelledError:
            self._drop_prefetch(key, tmpdir)
            raise
        except Exception as e:
            logger.debug("预取失败 msg=%s: %s", msg_id, e)
            self._drop_prefetch(key, tmpdir)

    def _drop_prefetch(self, key, tmpdir: str | None = None) -> None:
        entry = self._prefetch.pop(key, None)
        for d in {(entry or {}).get("tmpdir"), tmpdir}:
            if d:
                shutil.rmtree(d, ignore_errors=True)

    async def _take_prefetch(self, source_chat_id: int,
                             msg_id: int) -> tuple[str, str] | None:
        """取走预取结果；若还在下载就等它完成（此时已下载了一段时间）。"""
        entry = self._prefetch.pop((source_chat_id, msg_id), None)
        if not entry:
            return None
        task = entry.get("task")
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=300)
            except Exception as e:
                logger.debug("等待预取完成失败 msg=%s: %s", msg_id, e)
                self._discard_entry(entry)
                return None
        path, tmpdir = entry.get("path"), entry.get("tmpdir")
        if path and os.path.exists(path):
            return path, tmpdir
        self._discard_entry(entry)
        return None

    @staticmethod
    def _discard_entry(entry: dict) -> None:
        task = entry.get("task")
        if task and not task.done():
            task.cancel()
        if entry.get("tmpdir"):
            shutil.rmtree(entry["tmpdir"], ignore_errors=True)

    def cleanup_prefetch(self) -> None:
        """任务结束/异常时清掉所有预取任务与临时目录。"""
        for key in list(self._prefetch):
            entry = self._prefetch.pop(key)
            self._discard_entry(entry)


    async def _try_userbot_download_bot_upload_album(self, source_chat_id, msg_ids,
                                                     target_chat_id, topic_id,
                                                     userbot: TelegramClient | None = None) -> list[int]:
        ub = userbot or self.userbot
        await self.rl.wait()
        try:
            msgs = await self._get_message_list(ub, source_chat_id, msg_ids)
            if not msgs:
                logger.info("策略3相册: UserBot 无法获取消息 %s", msg_ids)
                return []
            return await self._with_fresh_ref_list(
                ub, source_chat_id, msg_ids,
                lambda ms: self._download_album_and_upload(
                    ub, ms, target_chat_id, topic_id),
                label="策略3相册", initial=msgs)
        except errors.FloodWaitError as e:
            return await self._handle_flood(
                e, self._try_userbot_download_bot_upload_album,
                source_chat_id, msg_ids, target_chat_id, topic_id, ub)
        except Exception as e:
            logger.warning("策略3相册: 异常: %s", e)
            return []

    async def _download_album_and_upload(self, ub: TelegramClient, msgs: list,
                                         target_chat_id: int,
                                         topic_id: int | None) -> list[int]:
        reply_to = self._reply_to(topic_id)
        media_msgs = [m for m in msgs if is_file_media(m)]
        if not media_msgs:
            logger.info("策略3相册: 无可下载媒体")
            return []

        with tempfile.TemporaryDirectory() as tmpdir:
            ok_items = await self._download_album_media(media_msgs, tmpdir, userbot=ub)
            if not ok_items:
                logger.warning("策略3相册: 媒体下载失败")
                return []

            ordered_items = sorted(ok_items, key=lambda item: item[0])
            captions = [m.text or "" for _, m, _, _ in ordered_items]

            files = []
            for _, message, path, thumb_path in ordered_items:
                item = await self.media.prepare_album_media_item(
                    message, path, thumb_path=thumb_path
                )
                files.append(item)

            has_video = any(self.media.is_video_message(m) for m in media_msgs)
            logger.info("策略3相册上传: 待发 %d 个文件, has_video=%s supports_streaming=%s",
                        len(files), has_video, has_video)
            result = await self.media.send_file_with_compat(
                target_chat_id,
                files,
                caption=captions,
                reply_to=reply_to,
                part_size_kb=self.upload_part_size_kb,
                supports_streaming=has_video,
            )
            return self._extract_result_ids(result)

    async def build_source_link(self, source_chat_id: int, msg_id: int,
                                userbot: TelegramClient | None = None) -> str:
        """构造源消息链接（公开或私有）。"""
        ub = userbot or self.userbot
        try:
            entity = await ub.get_entity(source_chat_id)
            username = getattr(entity, "username", None)
            if username:
                return f"https://t.me/{username}/{msg_id}"
        except Exception:
            pass
        chat_id = str(source_chat_id).replace("-100", "")
        return f"https://t.me/c/{chat_id}/{msg_id}"

    async def send_fail_marker(
        self,
        source_chat_id,
        msg_id,
        target_chat_id,
        topic_id,
        reason: str | None = None,
        userbot: TelegramClient | None = None,
    ) -> int | None:
        try:
            link = await self.build_source_link(source_chat_id, msg_id, userbot=userbot)
            reason_suffix = f"（{reason}）" if reason else ""
            text = f"⚠️ 无法转发的消息{reason_suffix}: {link}\n#fail2forward"
            result = await self.bot.send_message(
                target_chat_id, text,
                reply_to=topic_id if topic_id else None)
            return result.id if result else None
        except Exception as e:
            logger.error("发送失败标记异常: msg=%s err=%s", msg_id, e)
            return None

    async def _download_album_media(self, media_msgs: list[Message], tmpdir: str,
                                    userbot: TelegramClient | None = None):
        sem = asyncio.Semaphore(self.album_download_concurrency)

        async def _download_one(index: int, message: Message):
            file_size = self.media.get_message_file_size(message) or 0
            is_large = (
                self.media.enable_fast_transfer
                and file_size >= self.media.fast_transfer_min_size_mb * 1024 * 1024
            )
            if is_large:
                # 大文件由 MediaTransferHelper 的 _large_download_lock 严格互斥，独占并发连接
                path = await self.media.download_media_to_path(message, tmpdir, userbot=userbot)
            else:
                # 小文件（如图片）通过 sem 保持并发轻量单连接下载
                async with sem:
                    path = await self.media.download_media_to_path(message, tmpdir, userbot=userbot)

            thumb_path = None
            if path and self.media.is_video_message(message):
                thumb_path = await self.media.download_video_thumb_to_path(
                    message, tmpdir, userbot=userbot
                )
            return index, message, path, thumb_path

        tasks = [
            asyncio.create_task(_download_one(idx, m))
            for idx, m in enumerate(media_msgs)
        ]
        downloaded = await asyncio.gather(*tasks, return_exceptions=True)

        ok_items: list[tuple[int, Message, str, str | None]] = []
        for item in downloaded:
            if isinstance(item, Exception):
                logger.warning("策略3相册: 并发下载异常: %s", item)
                continue
            index, message, path, thumb_path = item
            if path:
                ok_items.append((index, message, path, thumb_path))
        return ok_items

    @staticmethod
    def _build_album_upload_payload(
        ok_items: list[tuple],
    ) -> tuple[list[str], list[str]]:
        ordered_items = sorted(ok_items, key=lambda item: item[0])
        files = [item[2] for item in ordered_items]
        captions = [item[1].text or "" for item in ordered_items]
        return files, captions

    async def _copy_message(self, client: TelegramClient, msg: Message,
                            target_chat_id: int, topic_id: int | None):
        reply_to = topic_id if topic_id else None
        if is_file_media(msg):
            send_kwargs = {}
            is_video = self.media.is_video_message(msg)
            if is_video:
                send_kwargs["supports_streaming"] = True
                self.media.ensure_video_streaming(msg)
                attrs = self.media.get_document_attributes(msg)
                if attrs:
                    send_kwargs["attributes"] = attrs
            logger.info("单条消息复制: msg=%s has_video=%s supports_streaming=%s",
                        msg.id, is_video, bool(send_kwargs.get("supports_streaming")))
            return await client.send_file(
                target_chat_id, msg.media,
                caption=msg.text or "",
                reply_to=reply_to,
                **send_kwargs)
        elif msg.text:
            return await client.send_message(
                target_chat_id, msg.text, reply_to=reply_to)
        return None

    async def _copy_album(self, client: TelegramClient, msgs: list[Message],
                          target_chat_id: int, topic_id: int | None):
        reply_to = topic_id if topic_id else None
        media_msgs = [m for m in msgs if is_file_media(m)]
        if not media_msgs:
            return None

        has_video = any(self.media.is_video_message(m) for m in media_msgs)
        for m in media_msgs:
            if self.media.is_video_message(m):
                self.media.ensure_video_streaming(m)

        if len(media_msgs) == 1:
            m = media_msgs[0]
            send_kwargs = {}
            is_video = self.media.is_video_message(m)
            if is_video:
                send_kwargs["supports_streaming"] = True
                attrs = self.media.get_document_attributes(m)
                if attrs:
                    send_kwargs["attributes"] = attrs
            logger.info("相册单条复制: msg=%s has_video=%s supports_streaming=%s",
                        m.id, is_video, bool(send_kwargs.get("supports_streaming")))
            return await client.send_file(
                target_chat_id, m.media, caption=m.text or "", reply_to=reply_to, **send_kwargs)

        files = [m.media for m in media_msgs]
        captions = [m.text or "" for m in media_msgs]
        send_kwargs = {}
        if has_video:
            send_kwargs["supports_streaming"] = True
        logger.info("相册多条复制: count=%d has_video=%s supports_streaming=%s",
                    len(media_msgs), has_video, bool(send_kwargs.get("supports_streaming")))
        return await client.send_file(
            target_chat_id, files, caption=captions, reply_to=reply_to, **send_kwargs)

    @staticmethod
    def _extract_result_ids(result) -> list[int]:
        if not result:
            return []
        if isinstance(result, list):
            return [m.id for m in result if m]
        if getattr(result, "id", None):
            return [result.id]
        return []

    async def _handle_flood(self, error: errors.FloodWaitError,
                            retry_func, *args):
        wait_seconds = error.seconds
        if wait_seconds > self.rl.max_flood_wait:
            logger.error("FloodWait %ds 超过最大等待 %ds，放弃", wait_seconds, self.rl.max_flood_wait)
            return None
        logger.warning("FloodWaitError: 等待 %ds 后重试", wait_seconds)
        self.rl.on_flood_wait()
        await asyncio.sleep(wait_seconds)
        return await retry_func(*args)

    async def _handle_userbot_flood(self, error: errors.FloodWaitError,
                                    retry_func, *args):
        """UserBot 路径的 FloodWait：多留余量、累计计数，超限就本次运行停用策略2。

        用户账号被限流后继续贴着官方秒数重试，是最容易被升级为「临时限制/强制下线」
        的做法，所以这里额外加缓冲，并在连续命中后主动退回 Bot/下载路径。
        """
        self._ub_floods += 1
        if self._ub_floods >= self.userbot_max_floods:
            if not self._ub_forward_disabled:
                self._ub_forward_disabled = True
                logger.error(
                    "UserBot 连续 %d 次 FloodWait -> 本次运行停用策略2，后续改走 Bot/下载路径"
                    "（重启进程或换账号后恢复）", self._ub_floods)
            return None
        wait_seconds = int(error.seconds) + self.userbot_flood_buffer
        if wait_seconds > self.rl.max_flood_wait + self.userbot_flood_buffer:
            logger.error("UserBot FloodWait %ds 超过最大等待，放弃本条", wait_seconds)
            return None
        logger.warning("UserBot FloodWait: 等待 %ds 后重试（连续第 %d 次）",
                       wait_seconds, self._ub_floods)
        self.rl.on_flood_wait()
        await asyncio.sleep(wait_seconds)
        return await retry_func(*args)

    @staticmethod
    def _reply_to(topic_id: int | None) -> int | None:
        return topic_id if topic_id else None

    @staticmethod
    def _reply_kwargs(topic_id: int | None) -> dict:
        return {"reply_to": topic_id} if topic_id else {}

    async def _get_single_message(self, client: TelegramClient,
                                  chat_or_entity, msg_id: int) -> Message | None:
        msg = await client.get_messages(chat_or_entity, ids=msg_id)
        return msg if msg else None

    async def _refetch_message(self, client: TelegramClient,
                               source_chat_id, msg_id: int) -> Message | None:
        """重取消息以拿到新的 file_reference（服务端每次都会下发最新引用）。"""
        try:
            return await self._get_single_message(client, source_chat_id, msg_id)
        except Exception as e:
            logger.warning("重取消息失败 msg=%s: %s", msg_id, e)
            return None

    async def _with_fresh_ref(self, client: TelegramClient, source_chat_id,
                              msg_id: int, action, *, label: str = "",
                              initial: Message | None = None):
        """执行 action(msg)；媒体引用过期时重取消息刷新引用后再试。

        Telethon 不会自动刷新 file_reference：一条消息被引用超过一段时间
        （或被重新上传）后，旧引用会一直报 FILE_REFERENCE_EXPIRED。
        克隆大频道时后半段媒体成批失败，基本都是这个原因，而它原本被
        `except Exception` 吞掉，表现为「这条消息凭空消失」。
        """
        msg = initial or await self._get_single_message(client, source_chat_id, msg_id)
        if not msg:
            return None
        for attempt in range(self.file_ref_retries + 1):
            try:
                return await action(msg)
            except FILE_REF_ERRORS as e:
                if attempt >= self.file_ref_retries:
                    logger.warning("%s msg=%s 引用失效且重试 %d 次仍失败: %s",
                                   label, msg_id, self.file_ref_retries, e)
                    raise
                self.ref_retries += 1
                logger.info("%s msg=%s 媒体引用过期，重取消息刷新（第 %d/%d 次）",
                            label, msg_id, attempt + 1, self.file_ref_retries)
                await asyncio.sleep(1 + attempt)
                fresh = await self._refetch_message(client, source_chat_id, msg_id)
                if not fresh:
                    raise
                msg = fresh
        return None

    async def _with_fresh_ref_list(self, client: TelegramClient, source_chat_id,
                                   msg_ids: list[int], action, *,
                                   label: str = "", initial: list | None = None):
        """相册版的引用刷新重试。"""
        msgs = initial or await self._get_message_list(client, source_chat_id, msg_ids)
        if not msgs:
            return []
        for attempt in range(self.file_ref_retries + 1):
            try:
                return await action(msgs)
            except FILE_REF_ERRORS as e:
                if attempt >= self.file_ref_retries:
                    logger.warning("%s msgs=%s 引用失效且重试 %d 次仍失败: %s",
                                   label, msg_ids, self.file_ref_retries, e)
                    raise
                self.ref_retries += 1
                logger.info("%s msgs=%s 媒体引用过期，重取整组刷新（第 %d/%d 次）",
                            label, msg_ids, attempt + 1, self.file_ref_retries)
                await asyncio.sleep(1 + attempt)
                fresh = await self._get_message_list(
                    client, source_chat_id, msg_ids)
                if not fresh:
                    raise
                msgs = fresh
        return []

    async def _get_single_message_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_id: int,
    ) -> Message | None:
        msg = await self._get_single_message(self.bot, chat_or_entity, msg_id)
        if msg:
            return msg
        logger.info(
            "策略1探测: 高层 get_messages 未命中 chat=%s msg=%s ref=%s",
            source_chat_id, msg_id, self._describe_peer(chat_or_entity),
        )
        msgs = await self._raw_get_messages_for_bot(source_chat_id, chat_or_entity, [msg_id])
        return msgs[0] if msgs else None

    async def _get_message_list(self, client: TelegramClient,
                                chat_or_entity, msg_ids: list[int]) -> list[Message]:
        msgs = await client.get_messages(chat_or_entity, ids=msg_ids)
        return normalize_messages(msgs)

    async def _get_message_list_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_ids: list[int],
    ) -> list[Message]:
        msgs = await self._get_message_list(self.bot, chat_or_entity, msg_ids)
        found_ids = {m.id for m in msgs if getattr(m, "id", None) is not None}
        missing_ids = [mid for mid in msg_ids if mid not in found_ids]
        if not missing_ids:
            return msgs

        logger.info(
            "策略1探测: 高层 get_messages 部分未命中 chat=%s want=%s got=%s ref=%s missing=%s",
            source_chat_id, msg_ids, sorted(found_ids), self._describe_peer(chat_or_entity), missing_ids,
        )
        raw_msgs = await self._raw_get_messages_for_bot(source_chat_id, chat_or_entity, missing_ids)
        merged: dict[int, Message] = {
            m.id: m for m in msgs if getattr(m, "id", None) is not None
        }
        for msg in raw_msgs:
            if getattr(msg, "id", None) is not None:
                merged[msg.id] = msg
        return [merged[mid] for mid in msg_ids if mid in merged]

    @staticmethod
    def _clamp_part_size_kb(value) -> int:
        try:
            n = int(value)
        except Exception:
            n = 512
        return max(32, min(512, n))

    async def _resolve_source_for_bot(self, source_chat_id):
        try:
            return await self.bot.get_input_entity(source_chat_id)
        except Exception:
            pass

        try:
            src = await self.userbot.get_entity(source_chat_id)
        except Exception:
            return source_chat_id

        username = getattr(src, "username", None)
        if username:
            try:
                return await self.bot.get_input_entity(username)
            except Exception:
                pass
        return source_chat_id

    async def _raw_get_messages_for_bot(
        self,
        source_chat_id,
        chat_or_entity,
        msg_ids: list[int],
    ) -> list[Message]:
        input_channel = await self._resolve_input_channel_for_bot(chat_or_entity, source_chat_id)
        if not input_channel:
            logger.info(
                "策略1探测: 无法构造 InputChannel chat=%s ref=%s",
                source_chat_id, self._describe_peer(chat_or_entity),
            )
            return []

        try:
            result = await self.bot(GetMessagesRequest(
                channel=input_channel,
                id=[InputMessageID(mid) for mid in msg_ids],
            ))
        except Exception as e:
            logger.info(
                "策略1探测: channels.getMessages 失败 chat=%s msg_ids=%s peer=%s err=%s",
                source_chat_id, msg_ids, self._describe_peer(input_channel), e,
            )
            return []

        messages = normalize_messages(getattr(result, "messages", None))
        valid = [
            m for m in messages
            if getattr(m, "id", None) is not None and type(m).__name__ != "MessageEmpty"
        ]
        logger.info(
            "策略1探测: channels.getMessages chat=%s msg_ids=%s peer=%s -> %s",
            source_chat_id, msg_ids, self._describe_peer(input_channel),
            [getattr(m, "id", None) for m in valid],
        )
        return valid

    async def _resolve_input_channel_for_bot(self, chat_or_entity, source_chat_id):
        candidates = [chat_or_entity]
        if source_chat_id not in candidates:
            candidates.append(source_chat_id)

        for candidate in candidates:
            try:
                input_peer = await self.bot.get_input_entity(candidate)
            except Exception:
                continue
            if hasattr(input_peer, "channel_id") and hasattr(input_peer, "access_hash"):
                return input_peer

        try:
            entity = await self.bot.get_entity(chat_or_entity)
        except Exception:
            entity = None
        if isinstance(entity, Channel):
            try:
                return await self.bot.get_input_entity(entity)
            except Exception:
                pass
        return None

    @staticmethod
    def _describe_peer(peer) -> str:
        if peer is None:
            return "None"
        fields = []
        for name in ("channel_id", "chat_id", "user_id", "access_hash", "username", "title"):
            value = getattr(peer, name, None)
            if value is not None:
                fields.append(f"{name}={value}")
        return f"{type(peer).__name__}({', '.join(fields)})" if fields else type(peer).__name__

