"""实时监控：监听源的新消息并转发到目标。"""
import asyncio
import logging
import os

from telethon import TelegramClient, events, errors
from telethon.tl.types import MessageService

from core.base_component import ForwardingComponent
from core.message_logic import is_album_candidate
from db.database import Database
from db import models

logger = logging.getLogger("tg_forward_bot.monitor")

# 监控任务每收到一条消息就写库代价偏高，先在内存累加，按此间隔批量落盘
STATS_FLUSH_SECONDS = float(os.getenv("MONITOR_STATS_FLUSH_SECONDS", "5"))


class MonitorManager(ForwardingComponent):
    def __init__(self, bot: TelegramClient, userbot: TelegramClient,
                 db: Database, config: dict,
                 userbot_manager=None):
        super().__init__(bot, userbot, db, config, userbot_manager=userbot_manager)
        self._handlers: dict[int, tuple[callable, TelegramClient]] = {}
        self._album_buffers: dict[tuple[int, int], dict] = {}
        # 监控任务的内存计数：done/fail/skip/last_msg_id/last_error
        self._stats: dict[int, dict] = {}
        self._stats_tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 进度统计
    # ------------------------------------------------------------------

    def _stats_of(self, task_id: int) -> dict:
        return self._stats.setdefault(task_id, {
            "done": 0, "fail": 0, "skip": 0,
            "last_msg_id": 0, "last_error": None,
        })

    def bump_stats(self, task_id: int, *, done: int = 0, fail: int = 0,
                   skip: int = 0, msg_id: int | None = None,
                   error: str | None = None) -> None:
        """累加监控进度（内存），由定时任务批量写库。"""
        s = self._stats_of(task_id)
        s["done"] += done
        s["fail"] += fail
        s["skip"] += skip
        if msg_id and msg_id > s["last_msg_id"]:
            s["last_msg_id"] = int(msg_id)
        if error:
            s["last_error"] = str(error)[:200]

    async def flush_stats(self, task_id: int) -> None:
        """把内存计数落盘；失败时保留计数，下个周期重试。"""
        s = self._stats.get(task_id)
        if not s:
            return
        done, fail, skip = s["done"], s["fail"], s["skip"]
        last_id, err = s["last_msg_id"], s["last_error"]
        if not (done or fail or skip or last_id or err):
            return
        try:
            await models.update_progress(
                self.db, task_id,
                last_msg_id=last_id or None,
                done_delta=done, fail_delta=fail, skip_delta=skip,
                last_error=err)
        except Exception as e:
            logger.debug("监控 #%s 进度写库失败: %s", task_id, e)
            return
        s["done"] = s["fail"] = s["skip"] = 0
        s["last_error"] = None

    async def _stats_loop(self, task_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(STATS_FLUSH_SECONDS)
                await self.flush_stats(task_id)
        except asyncio.CancelledError:
            return

    async def get_live_stats(self, task_id: int) -> dict:
        """返回任务最新计数（内存未落盘部分 + 库内已落盘部分）。"""
        await self.flush_stats(task_id)
        task = await models.get_task(self.db, task_id) or {}
        return {
            "done": int(task.get("done_count") or 0),
            "fail": int(task.get("fail_count") or 0),
            "skip": int(task.get("skip_count") or 0),
            "last_msg_id": int(task.get("last_synced_msg_id") or 0),
            "last_error": task.get("last_error"),
            "updated_at": task.get("updated_at"),
        }

    def is_running(self, task_id: int) -> bool:
        return task_id in self._handlers

    def _new_album_buffer(self, source_chat_id: int, target_chat_id: int,
                          mode: str, target_topic_id: int | None,
                          userbot: TelegramClient | None = None) -> dict:
        return {
            "msgs": [],
            "flush_task": None,
            "source_chat_id": source_chat_id,
            "target_chat_id": target_chat_id,
            "mode": mode,
            "target_topic_id": target_topic_id,
            "userbot": userbot,
        }

    @staticmethod
    def _match_source_topic(message, source_topic_id: int | None) -> bool:
        """判断消息是否属于指定 source topic。

        General 话题 (id=1) 特殊处理：Telegram API 中 General 话题的消息
        不携带 reply_to 指向话题 1，行为与普通群消息一致。因此判断逻辑为
        "不属于任何其他话题的消息即属于 General 话题"。
        """
        if not source_topic_id:
            return True
        reply_to = getattr(message, "reply_to", None)
        if source_topic_id == 1:
            # General 话题：无 reply_to 或 reply_to 不指向其他话题
            if not reply_to:
                return True
            top_id = getattr(reply_to, "reply_to_top_id", None)
            if top_id:
                return top_id == 1
            if getattr(reply_to, "forum_topic", False):
                return getattr(reply_to, "reply_to_msg_id", None) in (1, None)
            return True
        if not reply_to:
            return False
        top_msg_id = (
            getattr(reply_to, "reply_to_top_id", None)
            or getattr(reply_to, "reply_to_msg_id", None)
        )
        return top_msg_id == source_topic_id

    async def _forward_and_save(self, task_id: int, source_chat_id: int, source_msg_id: int,
                                target_chat_id: int, mode: str,
                                target_topic_id: int | None,
                                userbot: TelegramClient | None = None) -> bool:
        target_msg_id = await self.forwarder.forward_message(
            source_chat_id, source_msg_id, target_chat_id, mode, target_topic_id,
            caller="monitor", userbot=userbot)
        if target_msg_id:
            await models.save_message_map(self.db, task_id, source_msg_id, target_msg_id)
            return True
        return False

    async def _flush_album(self, task_id: int, grouped_id: int):
        key = (task_id, grouped_id)
        buffer = self._album_buffers.pop(key, None)
        if not buffer:
            return
        msgs = sorted(buffer["msgs"], key=lambda m: m.id)
        msg_ids = [m.id for m in msgs]
        source_chat_id = buffer["source_chat_id"]
        target_chat_id = buffer["target_chat_id"]
        mode = buffer["mode"]
        target_topic_id = buffer["target_topic_id"]
        ub = buffer.get("userbot")

        restricted, restricted_field = await self.forwarder.detect_restriction(
            source_chat_id, msg_ids[0], userbot=ub
        )
        if restricted:
            logger.info(
                "监控 #%s 相册受限 grouped_id=%s field=%s",
                task_id, grouped_id, restricted_field
            )
            await self.forwarder.send_fail_marker(
                source_chat_id=source_chat_id,
                msg_id=msg_ids[0],
                target_chat_id=target_chat_id,
                topic_id=target_topic_id,
                reason=f"受限: {restricted_field}",
                userbot=ub,
            )
            self.bump_stats(task_id, skip=len(msg_ids), msg_id=msg_ids[-1])
            return

        logger.info("监控 #%s 聚合相册 grouped_id=%s 条数=%s",
                    task_id, grouped_id, len(msg_ids))
        target_msg_ids = await self.forwarder.forward_album(
            source_chat_id, msg_ids, target_chat_id, mode, target_topic_id,
            caller="monitor", userbot=ub)
        ok = 0
        for src_id, tgt_id in zip(msg_ids, target_msg_ids):
            if tgt_id:
                await models.save_message_map(self.db, task_id, src_id, tgt_id)
                ok += 1
        self.bump_stats(task_id, done=ok, fail=len(msg_ids) - ok,
                        msg_id=msg_ids[-1])

    async def start_monitor(self, task_id: int, source_chat_id: int,
                            target_chat_id: int, mode: str = "copy",
                            source_topic_id: int | None = None,
                            target_topic_id: int | None = None,
                            userbot: TelegramClient | None = None):
        """启动一个监控任务。"""
        active_userbot = await self.get_active_userbot(source_chat_id, userbot)

        async def handler(event):
            # 跳过系统消息
            if isinstance(event.message, MessageService):
                return
            # 如果指定了 topic，只处理该 topic 的消息
            if not self._match_source_topic(event.message, source_topic_id):
                return

            logger.info("监控 #%s 收到新消息: chat=%s msg=%s",
                        task_id, source_chat_id, event.message.id)
            try:
                restricted, restricted_field = await self.forwarder.detect_restriction(
                    source_chat_id, event.message.id, userbot=active_userbot
                )
                if restricted:
                    logger.info(
                        "监控 #%s 受限消息 msg=%s field=%s",
                        task_id, event.message.id, restricted_field
                    )
                    await self.forwarder.send_fail_marker(
                        source_chat_id=source_chat_id,
                        msg_id=event.message.id,
                        target_chat_id=target_chat_id,
                        topic_id=target_topic_id,
                        reason=f"受限: {restricted_field}",
                        userbot=active_userbot,
                    )
                    self.bump_stats(task_id, skip=1, msg_id=event.message.id)
                    return

                grouped_id = getattr(event.message, "grouped_id", None)
                if grouped_id and is_album_candidate(event.message):
                    key = (task_id, grouped_id)
                    buffer = self._album_buffers.get(key)
                    if not buffer:
                        buffer = self._new_album_buffer(
                            source_chat_id, target_chat_id, mode, target_topic_id,
                            userbot=active_userbot)
                        self._album_buffers[key] = buffer
                    if all(m.id != event.message.id for m in buffer["msgs"]):
                        buffer["msgs"].append(event.message)
                    prev_task = buffer.get("flush_task")
                    if prev_task and not prev_task.done():
                        prev_task.cancel()

                    async def delayed_flush():
                        try:
                            await asyncio.sleep(1.5)
                            await self._flush_album(task_id, grouped_id)
                        except asyncio.CancelledError:
                            return
                        except Exception as e:
                            logger.warning("监控 #%s 相册处理异常: %s", task_id, e)
                            self.bump_stats(task_id, fail=1, error=str(e))

                    buffer["flush_task"] = asyncio.create_task(delayed_flush())
                    return

                ok = await self._forward_and_save(
                    task_id, source_chat_id, event.message.id,
                    target_chat_id, mode, target_topic_id,
                    userbot=active_userbot)
                self.bump_stats(
                    task_id, done=1 if ok else 0, fail=0 if ok else 1,
                    msg_id=event.message.id,
                    error=None if ok else "转发失败（详见容器日志）")
            except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
                logger.error("监控 #%s 源不可访问: %s，标记为失败", task_id, type(e).__name__)
                self.bump_stats(task_id, fail=1, error=f"源不可访问: {type(e).__name__}")
                await self.flush_stats(task_id)
                await models.update_task_status(self.db, task_id, "failed")
                self._handlers.pop(task_id, None)
                stats_task = self._stats_tasks.pop(task_id, None)
                if stats_task and not stats_task.done():
                    stats_task.cancel()
                active_userbot.remove_event_handler(handler)
                try:
                    await self.bot.send_message(
                        target_chat_id,
                        f"❌ 监控任务 #{task_id} 已失败: 源不可访问（可能已变为私有或被封禁）",
                        reply_to=target_topic_id if target_topic_id else None)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("监控 #%s 转发异常: %s", task_id, e)
                self.bump_stats(task_id, fail=1, msg_id=event.message.id,
                                error=f"{type(e).__name__}: {e}")

        active_userbot.add_event_handler(
            handler, events.NewMessage(chats=source_chat_id))
        self._handlers[task_id] = (handler, active_userbot)
        # 单个任务一个落盘协程，避免每条消息都写库
        self._stats_tasks[task_id] = asyncio.create_task(self._stats_loop(task_id))
        phone = getattr(active_userbot, "_phone", "default")
        logger.info("监控任务 #%s 已注册在 UserBot [%s]: source=%s topic=%s",
                    task_id, phone, source_chat_id, source_topic_id)

    async def stop_monitor(self, task_id: int):
        entry = self._handlers.pop(task_id, None)
        if entry:
            handler, client = entry
            client.remove_event_handler(handler)
        stats_task = self._stats_tasks.pop(task_id, None)
        if stats_task and not stats_task.done():
            stats_task.cancel()
        await self.flush_stats(task_id)
        keys = [k for k in self._album_buffers if k[0] == task_id]
        for key in keys:
            buf = self._album_buffers.pop(key, None)
            if not buf:
                continue
            flush_task = buf.get("flush_task")
            if flush_task and not flush_task.done():
                flush_task.cancel()
        await models.update_task_status(self.db, task_id, "paused")
        logger.info("监控任务 #%s 已停止", task_id)

    async def restore_tasks(self):
        """启动时恢复之前运行中的 monitor 任务。"""
        tasks = await models.get_tasks_by_status(self.db, "running")
        restored = 0
        for t in tasks:
            if t["type"] == "monitor":
                await self.start_monitor(
                    t["id"], t["source_chat_id"], t["target_chat_id"],
                    t["mode"], t["source_topic_id"], t["target_topic_id"])
                restored += 1
        if restored:
            logger.info("已恢复 %d 个监控任务", restored)

