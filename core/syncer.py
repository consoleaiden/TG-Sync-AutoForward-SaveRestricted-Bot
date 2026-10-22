"""历史克隆/同步：流式分批读取源消息并转发到目标。

设计要点
--------
1. **流式**：不再一次性把整个频道读进内存，按 `clone.batch_size` 分批
   拉取 + 处理，几万条的历史频道也能稳定跑。
2. **起点可选**：从头 / 从指定消息 ID / 从指定时间，通过任务表的
   `start_msg_id` / `start_date` 传入；解析为统一的 `offset_id`。
3. **断点续传**：每处理完一个转发单元就把 `last_synced_msg_id` 与计数
   写库，进程重启/手动暂停后从游标继续，不会重头再来。
4. **进度可见**：进度条 + 百分比 + 成功/失败/跳过计数，实时编辑到任务
   消息上（不会刷屏）。
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient, errors
from telethon.tl.types import MessageService

from core.base_component import ForwardingComponent
from core.clone_plan import (
    describe_start,
    format_duration,
    parse_stored_date,
    progress_bar,
    split_album_tail,
)
from core.message_logic import (
    build_forward_units,
    detect_hard_restriction,
    is_chat_globally_restricted,
)
from core.restricted_syncer import RestrictedSyncer
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.syncer")

DEFAULT_BATCH_SIZE = 200          # 每批拉取消息数
DEFAULT_PROGRESS_EVERY = 50       # 每 N 条刷新一次进度
MAX_FLOOD_RETRIES = 5             # 单次运行内 FloodWait 最大重试轮数


@dataclass
class ClonePlan:
    """一次运行的计划：从哪里开始、还剩多少、是否为续传。"""
    offset_id: int
    total: int | None
    resumed: bool
    start_desc: str
    done_before: int

    def summary(self, task_id: int) -> str:
        head = "🔁 继续克隆任务" if self.resumed else "🚀 开始克隆任务"
        total_part = f"{self.total:,} 条" if self.total else "数量待统计"
        return (
            f"{head} #{task_id}\n"
            f"📍 起点: {self.start_desc}\n"
            f"📦 本次待处理: {total_part}"
        )


class Syncer(ForwardingComponent):
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict, userbot_manager=None):
        super().__init__(bot, userbot, db, config, userbot_manager=userbot_manager)
        clone_cfg = config.get("clone") or {}
        self.batch_size = max(1, int(clone_cfg.get("batch_size") or DEFAULT_BATCH_SIZE))
        self.progress_every = max(1, int(
            clone_cfg.get("progress_notify_every") or DEFAULT_PROGRESS_EVERY))

    # ------------------------------------------------------------------
    # 起点解析
    # ------------------------------------------------------------------

    async def _date_to_boundary_id(self, client: TelegramClient, chat_id: int,
                                   topic_id: int | None,
                                   when: datetime) -> int:
        """把时间点换算成消息 ID 边界（返回该时间点及之前的最后一条）。"""
        try:
            msgs = await client.get_messages(
                chat_id, limit=1, offset_date=when, reply_to=topic_id)
        except Exception as e:
            logger.warning("按时间定位消息边界失败 chat=%s when=%s: %s", chat_id, when, e)
            return 0
        msg = msgs[0] if isinstance(msgs, list) and msgs else msgs
        return int(getattr(msg, "id", 0) or 0)

    async def _count_remaining(self, client: TelegramClient, chat_id: int,
                               topic_id: int | None, offset_id: int) -> int | None:
        """统计游标之后还剩多少条（服务端 count，不拉消息体）。"""
        try:
            msgs = await client.get_messages(
                chat_id, limit=1, reply_to=topic_id, min_id=max(0, offset_id))
            return getattr(msgs, "total", None)
        except Exception as e:
            logger.debug("统计剩余消息数失败 chat=%s: %s", chat_id, e)
            return None

    async def build_plan(self, task: dict, client: TelegramClient,
                         chat_id: int, topic_id: int | None) -> ClonePlan:
        """根据任务配置与已有进度计算本次运行的起点。"""
        last = int(task.get("last_synced_msg_id") or 0)
        start_msg_id = int(task.get("start_msg_id") or 0)
        done_before = int(task.get("done_count") or 0)

        if last > 0:
            offset_id = last
            resumed = True
            start_desc = describe_start(resumed=True, done=done_before, last_msg_id=last)
        elif start_msg_id > 0:
            # 指定 ID 表示「从这一条开始」，因此游标回退一条（reverse 模式取 > offset_id）
            offset_id = max(0, start_msg_id - 1)
            resumed = False
            start_desc = describe_start(start_msg_id=start_msg_id)
        elif task.get("start_date"):
            when = parse_stored_date(task.get("start_date"))
            offset_id = await self._date_to_boundary_id(client, chat_id, topic_id, when) \
                if when else 0
            resumed = False
            start_desc = describe_start(start_date=str(task.get("start_date")))
        else:
            offset_id = 0
            resumed = False
            start_desc = describe_start()

        remaining = await self._count_remaining(client, chat_id, topic_id, offset_id)
        total = None
        if remaining is not None:
            total = remaining + (done_before if resumed else 0)
        elif task.get("total_count"):
            total = int(task["total_count"])

        return ClonePlan(offset_id=offset_id, total=total, resumed=resumed,
                         start_desc=start_desc, done_before=done_before)

    # ------------------------------------------------------------------
    # 分批流式读取
    # ------------------------------------------------------------------

    @staticmethod
    def _split_album_tail(buffer: list, batch_size: int) -> tuple[list, list]:
        return split_album_tail(buffer, batch_size)

    async def _iter_batches(self, task_id: int, client: TelegramClient,
                            chat_id: int, topic_id: int | None, offset_id: int,
                            counter: dict):
        """按批产出消息列表；相册不会被切到两批。"""
        buffer: list = []
        async for msg in client.iter_messages(
            chat_id, reverse=True, offset_id=offset_id, reply_to=topic_id,
        ):
            if self._cancel_flags.get(task_id):
                break
            if isinstance(msg, MessageService):
                counter["skip"] += 1
                continue
            if not getattr(msg, "text", None) and getattr(msg, "media", None) is None:
                # 空消息（无文本无媒体）无法复制，直接跳过但推进游标
                counter["skip"] += 1
                counter["last_id"] = msg.id
                continue
            buffer.append(msg)
            if len(buffer) > self.batch_size:
                ready, buffer = self._split_album_tail(buffer, self.batch_size)
                if ready:
                    yield ready
        if buffer:
            yield buffer

    # ------------------------------------------------------------------
    # 受限消息预检查
    # ------------------------------------------------------------------

    async def _chat_level_restricted(self, source_chat_id: int,
                                     client: TelegramClient) -> bool:
        try:
            entity = await client.get_entity(source_chat_id)
            return is_chat_globally_restricted(entity)
        except Exception:
            return False

    def _precheck_restricted_units(
        self,
        chat_globally_restricted: bool,
        all_msgs: list,
        units: list[tuple[str, list[int]]],
    ) -> tuple[dict[int, tuple[bool, str]], int]:
        """预检查每个转发单元是否受限，返回缓存与受限消息总数。"""
        msg_map = {m.id: m for m in all_msgs if m}
        restriction_cache: dict[int, tuple[bool, str]] = {}
        restricted_total = 0
        for _, source_ids in units:
            if not source_ids:
                continue
            anchor_id = source_ids[0]
            restricted, restricted_field = detect_hard_restriction(
                chat_globally_restricted, msg_map.get(anchor_id)
            )
            restriction_cache[anchor_id] = (restricted, restricted_field)
            if restricted:
                restricted_total += len(source_ids)
        return restriction_cache, restricted_total

    # ------------------------------------------------------------------
    # Takeout 内联转发受限消息
    # ------------------------------------------------------------------

    async def _open_takeout(self, userbot: TelegramClient | None = None):
        """懒加载开启 Takeout 会话。"""
        ub = userbot or self.userbot
        takeout = ub.takeout()
        return await takeout.__aenter__()

    @staticmethod
    async def _close_takeout(takeout) -> None:
        """安全关闭 Takeout，用 shield 防止被 task cancel 打断。"""
        if takeout is None:
            return
        try:
            await asyncio.shield(takeout.__aexit__(None, None, None))
        except Exception as e:
            logger.warning("关闭 Takeout 异常: %s", e)

    async def _forward_restricted_unit(
        self,
        takeout,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        target_topic_id: int | None,
        unit_kind: str,
        source_ids: list[int],
    ) -> tuple[int, int, list[int]]:
        """通过 Takeout 转发受限消息单元，返回 (msg_count, last_msg_id, target_ids)。"""
        real_chat_id = self._ensure_supergroup_id(source_chat_id)
        try:
            messages = await takeout.get_messages(real_chat_id, ids=source_ids)
        except Exception as e:
            logger.error("克隆任务 #%s Takeout 获取消息失败 ids=%s: %s", task_id, source_ids, e)
            return 0, source_ids[-1], []

        valid_msgs = [m for m in (messages if isinstance(messages, list) else [messages]) if m]
        if not valid_msgs:
            return 0, source_ids[-1], []

        await self.rl.wait()
        try:
            if unit_kind == "album":
                target_ids = await RestrictedSyncer._copy_album(
                    takeout, valid_msgs, target_chat_id, target_topic_id
                )
                for src_id, tgt_id in zip(source_ids, target_ids):
                    await models.save_message_map(self.db, task_id, src_id, tgt_id)
                return len(target_ids), source_ids[-1], target_ids

            msg = valid_msgs[0]
            tgt_id = await RestrictedSyncer._copy_single(
                takeout, msg, target_chat_id, target_topic_id
            )
            if tgt_id:
                await models.save_message_map(self.db, task_id, source_ids[0], tgt_id)
                return 1, source_ids[0], [tgt_id]
            return 0, source_ids[0], []
        except errors.FloodWaitError as e:
            logger.warning("克隆任务 #%s Takeout FloodWait %ds", task_id, e.seconds)
            await asyncio.sleep(e.seconds)
            return 0, source_ids[-1], []
        except Exception as e:
            logger.warning("克隆任务 #%s Takeout 转发异常 ids=%s: %s", task_id, source_ids, e)
            return 0, source_ids[-1], []

    async def _forward_unit(
        self,
        task_id: int,
        source_chat_id: int,
        target_chat_id: int,
        mode: str,
        target_topic_id: int | None,
        unit_kind: str,
        source_ids: list[int],
        userbot: TelegramClient | None = None,
    ) -> tuple[int, int, list[int]]:
        """转发普通消息单元，返回 (msg_count, last_msg_id, target_ids)。"""
        if unit_kind == "album":
            target_ids = await self.forwarder.forward_album(
                source_chat_id, source_ids, target_chat_id, mode, target_topic_id,
                caller="syncer", userbot=userbot
            )
            for src_id, tgt_id in zip(source_ids, target_ids):
                await models.save_message_map(self.db, task_id, src_id, tgt_id)
            return len(target_ids), source_ids[-1], target_ids

        source_msg_id = source_ids[0]
        target_msg_id = await self.forwarder.forward_message(
            source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id,
            caller="syncer", userbot=userbot
        )
        if target_msg_id:
            await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
            return 1, source_msg_id, [target_msg_id]
        return 0, source_msg_id, []

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def start_sync(self, task_id: int, source_chat_id: int,
                         target_chat_id: int, mode: str = "copy",
                         source_topic_id: int | None = None,
                         target_topic_id: int | None = None,
                         notify_chat_id: int | None = None,
                         notify_topic_id: int | None = None,
                         notify_reply_to_msg_id: int | None = None,
                         userbot: TelegramClient | None = None):
        """执行历史克隆任务（支持指定起点与断点续传）。"""
        self._cancel_flags[task_id] = False
        active_userbot = await self.get_active_userbot(source_chat_id, userbot)
        phone = getattr(active_userbot, "_phone", "default")

        task = await models.get_task(self.db, task_id) or {}
        # 参数优先；进程重启后自动续传时回落到任务里存的通知上下文
        notify_chat_id = notify_chat_id or task.get("notify_chat_id")
        notify_topic_id = notify_topic_id or task.get("target_topic_id")
        notify_msg_id = notify_reply_to_msg_id or task.get("notify_msg_id")

        async def _notify(text: str) -> None:
            await self._notify(notify_chat_id, notify_topic_id, notify_msg_id, text)

        async def _edit_progress(text: str) -> bool:
            """优先编辑同一条进度消息，避免刷屏。返回是否编辑成功。"""
            if notify_chat_id and notify_msg_id:
                try:
                    await self.bot.edit_message(notify_chat_id, notify_msg_id, text)
                    return True
                except errors.MessageNotModifiedError:
                    return True
                except Exception as e:
                    logger.debug("编辑进度消息失败: %s", e)
            logger.info("任务 #%s 进度 | %s", task_id, text.replace("\n", " | "))
            return False

        async def _report(text: str) -> None:
            """有进度消息就原地更新，没有（或消息被删）才新发一条。"""
            if notify_chat_id and notify_msg_id:
                if await _edit_progress(text):
                    return
            await _notify(text)

        plan = await self.build_plan(task, active_userbot, source_chat_id, source_topic_id)
        logger.info("克隆任务 #%s 启动: source=%s topic=%s offset=%s total=%s resumed=%s (UserBot: %s)",
                    task_id, source_chat_id, source_topic_id, plan.offset_id,
                    plan.total, plan.resumed, phone)
        await models.update_progress(self.db, task_id, total=plan.total)
        header = plan.summary(task_id)
        src_label = task.get("source_title") or source_chat_id
        tgt_label = task.get("target_title") or target_chat_id
        header += f"\n📌 源: {src_label}\n🎯 目标: {tgt_label}"
        await _report(header)

        chat_restricted = await self._chat_level_restricted(source_chat_id, active_userbot)

        done = int(task.get("done_count") or 0)
        failed = int(task.get("fail_count") or 0)
        skipped = int(task.get("skip_count") or 0)
        cursor = plan.offset_id
        started_at = datetime.now().astimezone()
        last_progress_at = 0
        restricted_success = restricted_fail = 0
        takeout = None
        flood_retries = 0
        # 跨批次/跨 FloodWait 重试保留，确保跳过的计数不丢
        counter = {"last_id": cursor, "skip": 0}

        async def _render() -> str:
            elapsed = (datetime.now().astimezone() - started_at).total_seconds()
            speed = (done - plan.done_before) / elapsed if elapsed > 0 else 0
            eta = ((plan.total - done) / speed) if (plan.total and speed > 0) else None
            return (
                f"📥 克隆任务 #{task_id}\n"
                f"📌 源: {src_label}\n"
                f"🎯 目标: {tgt_label}\n"
                f"进度: {progress_bar(done, plan.total)}\n"
                f"游标: msg_id={cursor} · 速率 {speed:.1f} 条/秒\n"
                f"成功 {done - plan.done_before:,} · 失败 {failed:,} · 跳过 {skipped:,}\n"
                f"已用 {format_duration(elapsed)} · 剩余约 {format_duration(eta)}"
            )

        try:
            while True:
                try:
                    async for batch in self._iter_batches(
                        task_id, active_userbot, source_chat_id,
                        source_topic_id, cursor, counter,
                    ):
                        if self._cancel_flags.get(task_id):
                            break

                        units = build_forward_units(batch)
                        restriction_cache, _ = self._precheck_restricted_units(
                            chat_restricted, batch, units)

                        for kind, source_ids in units:
                            if self._cancel_flags.get(task_id):
                                break

                            is_restricted, _field = restriction_cache.get(
                                source_ids[0], (False, ""))

                            if is_restricted:
                                if takeout is None:
                                    try:
                                        takeout = await self._open_takeout(active_userbot)
                                    except Exception as e:
                                        logger.error("克隆任务 #%s Takeout 开启失败: %s", task_id, e)
                                        # 降级：跳过该单元，但推进游标避免死循环
                                        cursor = source_ids[-1]
                                        failed += len(source_ids)
                                        await models.update_progress(
                                            self.db, task_id, last_msg_id=cursor,
                                            fail_delta=len(source_ids))
                                        continue
                                ok_count, last_id, target_ids = await self._forward_restricted_unit(
                                    takeout, task_id, source_chat_id,
                                    target_chat_id, target_topic_id, kind, source_ids)
                                cursor = last_id
                                if target_ids:
                                    done += ok_count
                                    restricted_success += ok_count
                                    await models.update_progress(
                                        self.db, task_id, last_msg_id=cursor,
                                        done_delta=ok_count)
                                else:
                                    failed += len(source_ids)
                                    restricted_fail += len(source_ids)
                                    await models.update_progress(
                                        self.db, task_id, last_msg_id=cursor,
                                        fail_delta=len(source_ids))
                                self._log_forward_result(
                                    logger, "clone", task_id, kind, source_ids,
                                    target_ids, restricted=True)
                            else:
                                ok_count, last_id, target_ids = await self._forward_unit(
                                    task_id=task_id,
                                    source_chat_id=source_chat_id,
                                    target_chat_id=target_chat_id,
                                    mode=mode,
                                    target_topic_id=target_topic_id,
                                    unit_kind=kind,
                                    source_ids=source_ids,
                                    userbot=active_userbot,
                                )
                                cursor = last_id
                                if target_ids:
                                    done += ok_count
                                    await models.update_progress(
                                        self.db, task_id, last_msg_id=cursor,
                                        done_delta=ok_count)
                                else:
                                    failed += len(source_ids)
                                    await models.update_progress(
                                        self.db, task_id, last_msg_id=cursor,
                                        fail_delta=len(source_ids))
                                self._log_forward_result(
                                    logger, "clone", task_id, kind, source_ids, target_ids)

                            if done - last_progress_at >= self.progress_every:
                                last_progress_at = done
                                await _edit_progress(await _render())

                        # 批次间把跳过的消息计数与游标落盘
                        if counter["skip"]:
                            skipped += counter["skip"]
                            await models.update_progress(
                                self.db, task_id, skip_delta=counter["skip"],
                                last_msg_id=max(cursor, counter["last_id"]))
                            cursor = max(cursor, counter["last_id"])
                            counter["skip"] = 0

                    break  # 正常读完
                except errors.FloodWaitError as e:
                    flood_retries += 1
                    if flood_retries > MAX_FLOOD_RETRIES:
                        raise
                    logger.warning("克隆任务 #%s FloodWait %ds（第 %d 次），待恢复后从游标 %s 继续",
                                   task_id, e.seconds, flood_retries, cursor)
                    await asyncio.sleep(e.seconds + 1)
                    continue
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, ValueError) as e:
            # 源不可读（未加入私有频道、被踢、实体失效）→ 明确失败，别让任务静默挂掉
            logger.error("克隆任务 #%s 源不可访问: %s %s", task_id, type(e).__name__, e)
            await models.update_progress(
                self.db, task_id, last_msg_id=cursor,
                last_error=f"{type(e).__name__}: {e}"[:200])
            await models.update_task_status(self.db, task_id, "failed")
            await _report(f"❌ 克隆任务 #{task_id} 失败: {type(e).__name__}（游标保留在 msg {cursor}）")
            return
        except asyncio.CancelledError:
            await models.update_progress(self.db, task_id, last_msg_id=cursor)
            logger.info("克隆任务 #%s 被取消，游标停在 %s", task_id, cursor)
            raise
        finally:
            await self._close_takeout(takeout)

        await models.update_progress(self.db, task_id, last_msg_id=cursor)

        if self._cancel_flags.get(task_id):
            # 取消时可能还有未落盘的跳过计数，补写一次
            if counter["skip"]:
                skipped += counter["skip"]
                cursor = max(cursor, counter["last_id"])
                await models.update_progress(
                    self.db, task_id, skip_delta=counter["skip"], last_msg_id=cursor)
                counter["skip"] = 0
            await models.update_task_status(self.db, task_id, "paused")
            logger.info("克隆任务 #%s 已暂停，游标 %s（可随时续传）", task_id, cursor)
            await _report(
                f"⏸ 克隆任务 #{task_id} 已暂停\n"
                f"{progress_bar(done, plan.total)}\n"
                f"游标已保存: msg_id={cursor}（点「继续」即可接着跑）")
            return

        await models.update_task_status(self.db, task_id, "completed")
        logger.info("克隆任务 #%s 完成，本次新增 %d 条，累计成功 %d 条",
                    task_id, done - plan.done_before, done)

        restricted_line = f"\n• 受限(Takeout)成功: {restricted_success:,} 条"
        if restricted_fail:
            restricted_line += f"，失败 {restricted_fail:,} 条"
        await _report(
            "✅ 克隆完成"
            f"\n{progress_bar(done, plan.total or done)}"
            f"\n• 本次新增: {done - plan.done_before:,} 条"
            f"{restricted_line}"
            f"\n• 失败: {failed:,} 条 · 跳过: {skipped:,} 条"
            f"\n• 最终游标: msg_id={cursor}")
