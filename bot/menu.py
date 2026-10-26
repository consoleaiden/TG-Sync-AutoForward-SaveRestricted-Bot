"""按钮式交互：把「记命令 + 敲参数」改造成向导式对话。

入口：/start、/menu 或任意位置点菜单按钮。所有步骤都可用按钮完成，
只有「源链接 / 消息 ID / 时间」这三类不得不手输的内容才需要发文字。

状态机存放在内存（进程重启后未完成的向导自动失效，已创建的任务不受
影响，仍可从「我的任务」继续）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from telethon import Button, events

from bot.handler_common import STATUS_EMOJI
from bot.telegram_utils import get_target_topic_id
from core.clone_plan import (
    human_count,
    parse_start_time,
    progress_bar,
    to_local_time,
)
from core.storage import audit_db_path, backup_settings, format_audit
from db import models
from db.backup import backup_database, human_size, restore_hint

logger = logging.getLogger("tg_forward_bot.menu")

WIZARD_TTL = 15 * 60       # 向导空闲超时（秒）
CALLBACK_PREFIX = "m:"

_STEP_ORDER = {
    "clone": ("src", "target", "start", "mode", "confirm"),
    "monitor": ("src", "target", "mode", "confirm"),
}

# 需要手输内容的子步骤：归属于某个主步骤，但不占进度编号
_VIRTUAL_PARENT = {"from_id": "start", "from_time": "start"}

_KIND_LABEL = {"clone": "克隆历史消息", "monitor": "实时监控"}


def _is_callback(event) -> bool:
    """是否为按钮回调事件（只有回调消息才能被编辑）。"""
    try:
        return isinstance(event, events.CallbackQuery.Event)
    except TypeError:      # 某些 Telethon 版本 Event 不是可直接 isinstance 的类
        return getattr(event, "data", None) is not None


@dataclass
class Wizard:
    kind: str
    step: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def order(self) -> tuple[str, ...]:
        return _STEP_ORDER[self.kind]

    def goto(self, step: str) -> None:
        self.step = step
        self.ts = time.time()

    def next_step(self) -> str | None:
        step = _VIRTUAL_PARENT.get(self.step, self.step)
        order = self.order()
        idx = order.index(step)
        return order[idx + 1] if idx + 1 < len(order) else None

    def prev_step(self) -> str | None:
        # 手输子步骤返回时回到它的主步骤（起点选择页）
        if self.step in _VIRTUAL_PARENT:
            return _VIRTUAL_PARENT[self.step]
        order = self.order()
        idx = order.index(self.step)
        return order[idx - 1] if idx > 0 else None


class MenuController:
    """渲染菜单 + 驱动向导。业务动作通过注入的回调完成。"""

    def __init__(self, bot, *, db, config=None, userbot=None,
                 is_admin, format_display,
                 resolve_source, resolve_target,
                 create_task, stop_task, resume_task,
                 toggle_follow=None, continue_task=None,
                 monitor_manager=None):
        self.bot = bot
        self.db = db
        self.config = config or {}
        self.userbot = userbot
        self._is_admin = is_admin
        self._format_display = format_display
        self._resolve_source = resolve_source
        self._resolve_target = resolve_target
        self._create_task = create_task
        self._stop_task = stop_task
        self._resume_task = resume_task
        self._toggle_follow = toggle_follow
        self._continue_task = continue_task
        self.monitor_manager = monitor_manager
        self._wizards: dict[tuple[int, int], Wizard] = {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @staticmethod
    def _key(chat_id, user_id):
        return (int(chat_id or 0), int(user_id or 0))

    def has_pending(self, chat_id, user_id) -> bool:
        wiz = self._wizards.get(self._key(chat_id, user_id))
        if not wiz:
            return False
        if time.time() - wiz.ts > WIZARD_TTL:
            self._wizards.pop(self._key(chat_id, user_id), None)
            return False
        return True

    def drop(self, chat_id, user_id) -> None:
        self._wizards.pop(self._key(chat_id, user_id), None)

    def _get(self, event) -> Wizard | None:
        return self._wizards.get(self._key(event.chat_id, event.sender_id))

    def _start_wizard(self, event, kind: str) -> Wizard:
        wiz = Wizard(kind=kind, step="src")
        self._wizards[self._key(event.chat_id, event.sender_id)] = wiz
        return wiz

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _main_menu(self):
        text = (
            "🤖 TG 克隆助手\n"
            "把任意频道/群组的历史消息完整搬到你的频道，支持断点续传。\n\n"
            "请选择操作："
        )
        buttons = [
            [Button.inline("📥 新建克隆任务", data="m:new:clone")],
            [Button.inline("👁 新建持续监听", data="m:new:monitor")],
            [Button.inline("📋 克隆任务列表", data="m:tasks")],
            [Button.inline("📡 监听任务列表", data="m:monitors")],
            [Button.inline("💾 备份数据库", data="m:backup"),
             Button.inline("⚙️ 限流设置", data="m:settings")],
            [Button.inline("❓ 命令与说明", data="m:help")],
        ]
        return text, buttons

    async def _render_step(self, event, wiz: Wizard, edit: bool | None = None):
        step = wiz.step
        if step == "src":
            text, buttons = self._render_src(wiz)
        elif step == "start":
            text, buttons = self._render_start(wiz)
        elif step in ("from_id", "from_time"):
            text, buttons = self._render_start_input(wiz, step)
        elif step == "target":
            text, buttons = self._render_target(wiz)
        elif step == "mode":
            text, buttons = self._render_mode(wiz)
        elif step == "confirm":
            text, buttons = self._render_confirm(wiz)
        else:
            text, buttons = self._main_menu()

        # 只有点按钮（CallbackQuery）才能编辑原消息；用户发来的消息改为新发一条
        can_edit = _is_callback(event) if edit is None else edit
        if can_edit:
            try:
                await event.edit(text, buttons=buttons)
                return
            except Exception:
                pass
        await event.respond(text, buttons=buttons)

    def _render_src(self, wiz: Wizard):
        label = _KIND_LABEL[wiz.kind]
        text = (
            f"📥 {label} · 第 1 步 / 共 {len(wiz.order())} 步\n\n"
            "请把源频道/群组的链接发给我：\n"
            "• https://t.me/xxxxx （公开频道）\n"
            "• https://t.me/c/123456789/1 （私有频道）\n"
            "• 也可以直接发 @用户名\n\n"
            "支持带话题的链接，只克隆该话题。"
        )
        buttons = [[Button.inline("« 返回主菜单", data="m:home")]]
        return text, buttons

    def _render_start(self, wiz: Wizard):
        src = wiz.data.get("source") or {}
        total = src.get("total")
        lines = [
            f"📌 源: {src.get('title', '未知')}",
            f"📦 消息总数: {human_count(total)}",
            "",
            f"第 {self._step_no(wiz, 'start')} 步 · 选择克隆起点",
        ]
        buttons = [[Button.inline("⏮ 从头开始（最早 → 最新）", data="m:start:head")]]
        if src.get("resumable"):
            r = src["resumable"]
            buttons.append([Button.inline(
                f"▶️ 继续上次进度（已到 msg {r.get('last_synced_msg_id')}）",
                data="m:start:resume")])
        buttons.append([
            Button.inline("🔢 从指定消息 ID", data="m:start:id"),
            Button.inline("🕒 从指定时间", data="m:start:time"),
        ])
        buttons.append([Button.inline("« 上一步", data="m:back")])
        return "\n".join(lines), buttons

    def _step_no(self, wiz: Wizard, step: str) -> int:
        try:
            return wiz.order().index(step) + 1
        except ValueError:
            return 1

    def _render_start_input(self, wiz: Wizard, step: str):
        if step == "from_id":
            text = (
                "🔢 请发送起始消息 ID（纯数字）。\n"
                "例如源频道链接是 t.me/xxx/1234，就发送 1234，将从这条开始往后克隆。"
            )
        elif step == "from_time":
            text = (
                "🕒 请发送起始时间，支持这些格式：\n"
                "• 2024-01-31\n"
                "• 2024-01-31 12:30\n"
                "• 2024/01/31\n"
                "• 20240131\n\n"
                "会从该时间点之后的第一条消息开始克隆。"
            )
        buttons = [[Button.inline("« 上一步", data="m:back")]]
        return text, buttons

    def _render_target(self, wiz: Wizard):
        src = wiz.data.get("source") or {}
        text = (
            f"📌 源: {src.get('title', '未知')}\n\n"
            f"第 {self._step_no(wiz, 'target')} 步 · 选择目标\n"
            "点下面的按钮发到当前会话，或直接把目标频道的链接发给我。\n"
            "（需要把 Bot 加为目标频道的管理员，否则无法发送）"
        )
        buttons = [
            [Button.inline("📤 发送到当前会话", data="m:target:here")],
            [Button.inline("« 上一步", data="m:back")],
        ]
        return text, buttons

    def _render_mode(self, wiz: Wizard):
        text = (
            f"第 {self._step_no(wiz, 'mode')} 步 · 选择转发模式\n\n"
            "• 复制：重新上传内容，不带「转发自」标记\n"
            "• 转发：保留原频道署名，速度快但会显示来源\n\n"
            "受限频道通常只能用「复制」。"
        )
        buttons = [
            [Button.inline("📄 复制内容（推荐）", data="m:mode:copy")],
            [Button.inline("🔁 转发消息（带来源）", data="m:mode:forward")],
            [Button.inline("« 上一步", data="m:back")],
        ]
        return text, buttons

    def _render_confirm(self, wiz: Wizard):
        src = wiz.data.get("source") or {}
        tgt = wiz.data.get("target") or {}
        start = wiz.data.get("start") or {}
        mode = wiz.data.get("mode") or "copy"
        lines = [
            "✅ 请确认任务信息",
            "",
            f"类型: {_KIND_LABEL[wiz.kind]}",
            f"源: {src.get('title', '未知')}",
            f"目标: {tgt.get('title', '未知')}",
        ]
        if wiz.kind == "clone":
            lines.append(f"起点: {start.get('desc', '从头开始')}")
            total = src.get("total")
            if start.get("kind") == "head" and total:
                lines.append(f"预计: {human_count(total)} 条消息")
        lines.append(f"模式: {'复制' if mode == 'copy' else '转发'}")
        lines.append("")
        if wiz.kind == "monitor":
            lines.append("点「开始」后立即开始监听新消息，源频道必须是 UserBot 已加入的。")
        else:
            lines.append("点「开始」后会在后台运行；跑完默认转成持续监听，"
                         "可在任务详情里关闭。")
        buttons = [
            [Button.inline("🚀 开始", data="m:go")],
            [Button.inline("« 上一步", data="m:back"),
             Button.inline("✖️ 取消", data="m:cancel")],
        ]
        return "\n".join(lines), buttons

    # ------------------------------------------------------------------
    # 任务列表 / 详情
    # ------------------------------------------------------------------

    @staticmethod
    def _task_icon(task: dict) -> str:
        return "📥" if task["type"] in models.CLONE_TASK_TYPES else "👁"

    @staticmethod
    def _pct_text(task: dict) -> str:
        total = int(task.get("total_count") or 0)
        done = int(task.get("done_count") or 0)
        if task["type"] not in models.CLONE_TASK_TYPES:
            # 监控任务没有总量，用已转发条数表示进度
            return f"{done:,} 条" if done else "待转发"
        if total > 0:
            return f"{min(100, done * 100 // total)}%"
        return "—"

    async def render_tasks(self, event, edit: bool = True):
        """克隆任务列表：已完成的也保留，并标出是否在持续监听。"""
        tasks = await models.get_tasks_for_list(
            self.db, models.CLONE_TASK_TYPES, limit=12)
        if not tasks:
            text = ("📭 还没有克隆任务\n\n"
                    "点「新建克隆任务」创建第一个，或用 /kl <频道链接>。")
            buttons = [[Button.inline("📥 新建克隆任务", data="m:new:clone")],
                       [Button.inline("« 主菜单", data="m:home")]]
        else:
            running = sum(1 for t in tasks if t["status"] == "running")
            following = sum(1 for t in tasks if int(t.get("follow") or 0))
            text = (f"📋 克隆任务（{len(tasks)} 个 · 运行中 {running} · "
                    f"持续监听 {following}）\n"
                    "已完成的记录会一直保留，点一项可看详情、开关持续监听。")
            buttons = []
            for t in tasks[:12]:
                status = STATUS_EMOJI.get(t["status"], "❓")
                name = t.get("source_title") or str(t["source_chat_id"])
                follow = "🛰" if int(t.get("follow") or 0) else ""
                label = (f"{status}📥{follow} #{t['id']} {name} "
                         f"{self._pct_text(t)}")
                buttons.append([Button.inline(label[:52], data=f"m:task:{t['id']}")])
            buttons.append([Button.inline("🔄 刷新", data="m:tasks"),
                            Button.inline("📡 监听列表", data="m:monitors")])
            buttons.append([Button.inline("« 主菜单", data="m:home")])
        if edit:
            try:
                await event.edit(text, buttons=buttons)
                return
            except Exception:
                pass
        await event.respond(text, buttons=buttons)

    async def _monitor_stats(self, task: dict) -> dict:
        """取监控任务进度：优先用运行时未落盘计数，失败则退回库内值。"""
        out = {
            "done": int(task.get("done_count") or 0),
            "fail": int(task.get("fail_count") or 0),
            "skip": int(task.get("skip_count") or 0),
            "last_msg_id": int(task.get("last_synced_msg_id") or 0),
            "last_error": task.get("last_error"),
            "last_activity": to_local_time(task.get("updated_at")),
        }
        mm = getattr(self, "monitor_manager", None)
        if mm is not None and hasattr(mm, "get_live_stats"):
            try:
                live = await mm.get_live_stats(task["id"])
                for key in ("done", "fail", "skip", "last_msg_id", "last_error"):
                    if live.get(key) is not None:
                        out[key] = live[key]
                if live.get("updated_at"):
                    out["last_activity"] = to_local_time(live["updated_at"])
            except Exception as e:
                logger.debug("读取监控实时进度失败 #%s: %s", task["id"], e)
        return out

    async def render_monitors(self, event, edit: bool = True):
        """监听任务总览：看得到设置了哪些监听、各自转发了多少。"""
        tasks = await models.get_tasks_by_type(self.db, "monitor")
        if not tasks:
            text = ("📡 还没有实时监控任务\n\n"
                    "监控会把源频道的新消息实时转发到目标，"
                    "创建后在这里能看到每个任务的源、目标与已转发条数。")
            buttons = [[Button.inline("👁 新建实时监控", data="m:new:monitor")],
                       [Button.inline("📥 新建克隆任务", data="m:new:clone")],
                       [Button.inline("« 主菜单", data="m:home")]]
        else:
            running = sum(1 for t in tasks if t["status"] == "running")
            lines = [f"📡 监听任务（共 {len(tasks)} 个，运行中 {running} 个）", ""]
            for t in tasks[:10]:
                stats = await self._monitor_stats(t)
                status = STATUS_EMOJI.get(t["status"], "❓")
                src = t.get("source_title") or str(t["source_chat_id"])
                tgt = t.get("target_title") or str(t["target_chat_id"])
                scope = f"话题 #{t['source_topic_id']}" if t.get("source_topic_id") else "全部消息"
                origin = (f"（来自克隆任务 #{t['parent_id']}）"
                          if t.get("parent_id") else "")
                lines.append(f"{status} #{t['id']} {src}（{scope}）{origin}")
                lines.append(f"   → {tgt}")
                lines.append(f"   已转发 {stats['done']:,} · 失败 {stats['fail']} · 跳过 {stats['skip']}")
                if stats.get("last_msg_id"):
                    lines.append(f"   最近消息 msg_id={stats['last_msg_id']}")
                if stats.get("last_activity"):
                    lines.append(f"   最近活动 {stats['last_activity']}")
                if stats.get("last_error"):
                    lines.append(f"   ⚠️ {str(stats['last_error'])[:60]}")
                lines.append("")
            buttons = []
            for t in tasks[:10]:
                status = STATUS_EMOJI.get(t["status"], "❓")
                src = (t.get("source_title") or str(t["source_chat_id"]))[:24]
                buttons.append([Button.inline(
                    f"{status} #{t['id']} {src} {self._pct_text(t)}"[:52],
                    data=f"m:task:{t['id']}")])
            buttons.append([Button.inline("🔄 刷新", data="m:monitors"),
                            Button.inline("👁 新建监控", data="m:new:monitor")])
            buttons.append([Button.inline("« 主菜单", data="m:home")])
            text = "\n".join(lines).rstrip()
        if edit:
            try:
                await event.edit(text, buttons=buttons)
                return
            except Exception:
                pass
        await event.respond(text, buttons=buttons)

    async def render_task_detail(self, event, task_id: int, edit: bool = True):
        t = await models.get_task(self.db, task_id)
        if not t:
            await event.answer("任务不存在", alert=True)
            return

        status = STATUS_EMOJI.get(t["status"], "❓")
        done = int(t.get("done_count") or 0)
        total = int(t.get("total_count") or 0) or None
        # 进度口径 = 已处理（成功+失败+跳过）/ 总数，全部处理完即为 100%；
        # 只用成功数会因为服务消息/去重跳过永远到不了 100%
        processed = (done + int(t.get("fail_count") or 0)
                     + int(t.get("skip_count") or 0))
        src_title = t.get("source_title") or await self._format_display(
            t["source_chat_id"], t["source_topic_id"])
        tgt_title = t.get("target_title") or await self._format_display(
            t["target_chat_id"], t["target_topic_id"], prefer_bot=True)
        start = self._describe_stored_start(t)

        lines = [
            f"{self._task_icon(t)} 任务 #{t['id']} · {_KIND_LABEL['clone'] if t['type'] in models.CLONE_TASK_TYPES else _KIND_LABEL['monitor']}",
            f"状态: {status} {t['status']}",
            f"源: {src_title}",
            f"目标: {tgt_title}",
        ]
        if t["type"] in models.CLONE_TASK_TYPES:
            lines.append(f"起点: {start}")
            lines.append(f"进度: {progress_bar(processed, total)}")
            lines.append(f"游标: msg_id={int(t.get('last_synced_msg_id') or 0)}")
            lines.append(
                f"成功 {done:,} · 失败 {int(t.get('fail_count') or 0):,}"
                f" · 跳过 {int(t.get('skip_count') or 0):,}")
            follow_on = bool(int(t.get("follow") or 0))
            mid = int(t.get("follow_task_id") or 0)
            follow_text = ("🛰 开启" + (f"（监听任务 #{mid}）" if mid else "")
                           if follow_on else "⏹ 关闭")
            lines.append(f"持续监听: {follow_text}")
        else:
            stats = await self._monitor_stats(t)
            scope = (f"仅话题 #{t['source_topic_id']}"
                     if t.get("source_topic_id") else "全部新消息")
            lines.append(f"监听范围: {scope}")
            lines.append(
                f"已转发 {stats['done']:,} 条 · 失败 {stats['fail']} · 跳过 {stats['skip']}")
            if stats.get("last_msg_id"):
                lines.append(f"最近消息: msg_id={stats['last_msg_id']}")
            if stats.get("last_activity"):
                lines.append(f"最近活动: {stats['last_activity']}")
        if t.get("last_error"):
            lines.append(f"最近错误: {str(t['last_error'])[:80]}")
        created = to_local_time(t.get("created_at"))
        updated = to_local_time(t.get("updated_at"))
        lines.append(f"创建 {created} · 更新 {updated}")

        buttons = []
        if t["status"] == "running":
            buttons.append([Button.inline("⏸ 暂停", data=f"m:pause:{t['id']}")])
        elif t["status"] in ("paused", "failed"):
            resume_label = ("▶️ 继续监听" if t["type"] not in models.CLONE_TASK_TYPES
                            else "▶️ 继续克隆")
            buttons.append([Button.inline(resume_label, data=f"m:resume:{t['id']}")])
        elif t["status"] == "completed" and t["type"] in models.CLONE_TASK_TYPES:
            buttons.append([Button.inline("🔁 继续补新消息",
                                          data=f"m:cont:{t['id']}")])
        if t["type"] in models.CLONE_TASK_TYPES:
            follow_on = bool(int(t.get("follow") or 0))
            buttons.append([Button.inline(
                "⏹ 关闭持续监听" if follow_on else "🛰 开启持续监听",
                data=f"m:follow:{t['id']}")])
        buttons.append([Button.inline("🔄 刷新", data=f"m:task:{t['id']}")])
        buttons.append([Button.inline("🗑 删除", data=f"m:del:{t['id']}"),
                        Button.inline("« 任务列表", data="m:tasks")])
        buttons.append([Button.inline("« 主菜单", data="m:home")])

        text = "\n".join(lines)
        if edit:
            try:
                await event.edit(text, buttons=buttons)
                return
            except Exception:
                pass
        await event.respond(text, buttons=buttons)

    @staticmethod
    def _describe_stored_start(task: dict) -> str:
        if task.get("start_date"):
            return f"从 {task['start_date']} 之后开始"
        if int(task.get("start_msg_id") or 0):
            return f"从消息 {task['start_msg_id']} 开始"
        return "从头开始（最早 → 最新）"

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self):
        bot = self.bot

        @bot.on(events.CallbackQuery(pattern=rb"^m:"))
        async def on_menu_callback(event):
            if not await self._is_admin(event):
                return
            data = event.data.decode()
            action = data[len(CALLBACK_PREFIX):]
            logger.info("菜单点击: user=%s chat=%s action=%s",
                        event.sender_id, event.chat_id, action)
            try:
                await self._dispatch(event, action)
            except Exception as e:
                logger.error("菜单处理异常 action=%s: %s", action, e, exc_info=True)
                await event.answer(f"操作失败: {e}", alert=True)

        @bot.on(events.NewMessage(func=lambda e: (
            not e.out
            and not (e.raw_text or "").startswith("/")
            and self.has_pending(e.chat_id, e.sender_id)
        )))
        async def on_wizard_text(event):
            wiz = self._get(event)
            if not wiz:
                return
            if not await self._is_admin(event):
                self.drop(event.chat_id, event.sender_id)
                # 之前是静默丢弃，用户会以为「机器人没反应」，这里明确回一条
                try:
                    await event.reply("⛔ 无权限：只有配置里的管理员可以创建/管理任务。")
                except Exception:
                    pass
                return
            try:
                await self._on_text(event, wiz)
            except events.StopPropagation:
                raise
            except Exception as e:
                logger.error("向导输入处理异常: %s", e, exc_info=True)
                await event.reply(f"❌ 处理失败: {e}")
            # 阻止后续「私聊链接解析」等处理器重复处理同一条消息
            raise events.StopPropagation

        return self

    async def _on_backup(self, event):
        """把 SQLite 在线快照到 data/backups（随 ./data 挂载留在宿主机）。"""
        db_path = getattr(self.db, "db_path", "data/bot.db")
        opts = backup_settings(self.config)
        target_dir = opts["backup_dir"] or os.path.join(
            os.path.dirname(os.path.abspath(db_path)) or ".", "backups")
        await event.answer("正在备份…")
        try:
            info = await asyncio.to_thread(
                backup_database, db_path,
                backup_dir=target_dir, keep=opts["keep"],
            )
        except FileNotFoundError:
            await event.answer("数据库文件还不存在，暂无内容可备份", alert=True)
            return
        except Exception as e:
            logger.error("数据库备份失败: %s", e, exc_info=True)
            await event.answer(f"备份失败: {e}", alert=True)
            return

        text = (
            "💾 数据库备份完成\n\n"
            f"• 文件：{info['path']}\n"
            f"• 大小：{human_size(info['size'])}\n"
            f"• 现有备份：{info['total']} 份（保留最近 {info['keep']} 份"
            f"{'，已清理 %d 份旧备份' % len(info['removed']) if info['removed'] else ''}）\n\n"
            "备份目录在挂载的 ./data 内，升级镜像不会丢。\n\n"
            "如需恢复：\n"
            f"{restore_hint(os.path.abspath(db_path), info['path'])}"
        )
        await event.edit(text, buttons=[
            [Button.inline("💾 再备份一份", data="m:backup"),
             Button.inline("« 主菜单", data="m:home")],
        ])

    async def _dispatch(self, event, action: str):
        if action == "home":
            self.drop(event.chat_id, event.sender_id)
            text, buttons = self._main_menu()
            await event.edit(text, buttons=buttons)
            return

        if action.startswith("new:"):
            kind = action.split(":", 1)[1]
            wiz = self._start_wizard(event, kind)
            await self._render_step(event, wiz)
            return

        if action == "tasks":
            await self.render_tasks(event)
            return

        if action == "monitors":
            await self.render_monitors(event)
            return

        if action == "backup":
            await self._on_backup(event)
            return

        if action.startswith("task:"):
            await self.render_task_detail(event, int(action.split(":")[1]))
            return

        if action.startswith("follow:"):
            task_id = int(action.split(":")[1])
            if not self._toggle_follow:
                await event.answer("当前版本不支持持续监听", alert=True)
                return
            ok, msg = await self._toggle_follow(task_id)
            await event.answer(msg, alert=not ok)
            await self.render_task_detail(event, task_id)
            return

        if action.startswith("cont:"):
            task_id = int(action.split(":")[1])
            if not self._continue_task:
                await event.answer("当前版本不支持继续克隆", alert=True)
                return
            ok, err = await self._continue_task(task_id)
            await event.answer("🔁 已继续，只补新消息" if ok else (err or "继续失败"),
                               alert=not ok)
            await self.render_task_detail(event, task_id)
            return

        if action.split(":", 1)[0] in ("pause", "resume", "del"):
            verb, _, raw_id = action.partition(":")
            task_id = int(raw_id)
            if verb == "pause":
                await self._stop_task(task_id)
                await models.update_task_status(self.db, task_id, "paused")
                await event.answer(f"⏸ 任务 #{task_id} 已暂停（游标已保存）")
            elif verb == "resume":
                ok, err = await self._resume_task(task_id)
                await event.answer("▶️ 已继续" if ok else (err or "无法继续"), alert=not ok)
            else:
                await self._stop_task(task_id)
                await models.update_task_status(self.db, task_id, "deleted")
                await event.answer(f"🗑 任务 #{task_id} 已删除（记录已移除）")
                await self.render_tasks(event)
                return
            await self.render_task_detail(event, task_id)
            return

        if action == "settings":
            rl = (self.config.get("rate_limit") or {})
            audit = audit_db_path(getattr(self.db, "db_path", "data/bot.db"),
                                  config=self.config)
            store_state = {
                True: "✅ 已挂载到宿主机，升级镜像不会丢",
                False: "⚠️ 未挂载，升级镜像会丢（见 docs/storage.md）",
                None: "本地磁盘，直接落盘",
            }[audit["persisted"]]
            text = (
                "⚙️ 当前限流设置\n"
                f"• 每批消息数: {rl.get('batch_size', 100)}\n"
                f"• 每条间隔: {rl.get('forward_interval', [2, 5])}\n"
                f"• 每 {rl.get('batch_pause_every', 50)} 条休息: "
                f"{rl.get('batch_pause_time', [30, 60])}\n"
                f"• FloodWait 倍增: {rl.get('flood_wait_multiplier', 2)}\n"
                f"• 最大等待: {rl.get('max_flood_wait', 300)}s\n\n"
                "💾 数据存储\n"
                f"• {format_audit(audit)}\n"
                f"• {store_state}\n\n"
                "修改 config.yaml 后立即生效，无需重启。\n"
                "⚡ 也可直接用 /sd 命令在线调速度阈值（bot / ub 分别设置）。"
            )
            await event.edit(text, buttons=[[Button.inline("« 主菜单", data="m:home")]])
            return

        if action == "help":
            text = (
                "❓ 命令与功能对照\n\n"
                "【克隆 / 监听】\n"
                "/kl <链接> — 克隆全部消息，完成后自动持续监听\n"
                "/kl 100-1100 <链接> — 只克隆 msg 100~1100\n"
                "/kljx <链接> — 接着上次进度补到最新，不重复\n"
                "/kljx <链接> 120 — 从消息 120 开始继续克隆\n"
                "/kljx <链接> 120-1000 — 只克隆 msg 120~1000\n"
                "/jt <链接> — 只监听新消息\n"
                "/kllb — 克隆任务列表（含已完成）\n"
                "/jtlb — 监听任务列表\n"
                "/klfail — 查看失败条目（带任务 ID 看明细）\n"
                "/retryfail <任务id|all> — 一键补发失败条目\n\n"
                "【速度】\n"
                "/sd — 查看克隆速度阈值\n"
                "/sd bot fast — Bot 提速；/sd bot max 全速；/sd bot safe 恢复默认\n"
                "/sd ub fast / safe — UserBot 档位\n"
                "/sd bot 0.3-0.8 / pause 100 20-40 — 自定义间隔与长歇\n"
                "/cfg <段.键> <值> — 覆盖任意配置项（存数据库，重启不丢）\n"
                "/cfg <段.键> reset — 恢复 config.yaml 的值\n"
                "/bf <数量> — 设置克隆并发数（1~10，默认 1，越大风控风险越高）\n\n"
                "【代理测速】\n"
                "/speedtop — 测速榜单前 40 名代理，测完自动热切换到最快的\n"
                "/speedall — 测速全量代理榜（约 1000 条，耗时数分钟）\n\n"
                "【工具】\n"
                "/chapd — 转发一条频道消息，识别来源频道\n"
                "/chaid — 转发一条频道消息，返回消息 ID 和链接（配 /kljx 用）\n"
                "/start — 打开本菜单\n\n"
                "💡 克隆任务记录会永久保留，跑完后默认转成持续监听，\n"
                "   在任务详情里可以随时开关「🛰 持续监听」。\n"
                "💡 克隆完成消息里会给出「覆盖率」，有缺口就用 /retryfail 补。\n"
                "💡 目标频道需要先把 Bot 设为管理员。\n"
                "💡 进度存在 data/bot.db，点「备份数据库」可导出快照。"
            )
            await event.edit(text, buttons=[[Button.inline("« 主菜单", data="m:home")]])
            return

        if action == "back":
            wiz = self._get(event)
            if not wiz:
                text, buttons = self._main_menu()
                await event.edit(text, buttons=buttons)
                return
            prev = wiz.prev_step()
            if not prev:
                self.drop(event.chat_id, event.sender_id)
                text, buttons = self._main_menu()
                await event.edit(text, buttons=buttons)
                return
            wiz.goto(prev)
            await self._render_step(event, wiz)
            return

        if action == "cancel":
            self.drop(event.chat_id, event.sender_id)
            text, buttons = self._main_menu()
            await event.edit("已取消。\n\n" + text, buttons=buttons)
            return

        if action.startswith("start:"):
            await self._on_start_choice(event, action.split(":", 1)[1])
            return

        if action.startswith("target:"):
            await self._on_target_choice(event, action.split(":", 1)[1])
            return

        if action.startswith("mode:"):
            wiz = self._get(event)
            if not wiz:
                return
            wiz.data["mode"] = action.split(":", 1)[1]
            nxt = wiz.next_step()
            wiz.goto(nxt or "confirm")
            await self._render_step(event, wiz)
            return

        if action == "go":
            await self._on_confirm(event)

    # ------------------------------------------------------------------
    # 各步骤交互
    # ------------------------------------------------------------------

    async def _on_start_choice(self, event, choice: str):
        wiz = self._get(event)
        if not wiz:
            return
        if choice == "head":
            wiz.data["start"] = {"kind": "head", "desc": "从头开始（最早 → 最新）"}
        elif choice == "resume":
            r = (wiz.data.get("source") or {}).get("resumable") or {}
            wiz.data["start"] = {
                "kind": "resume",
                "task_id": r.get("id"),
                "desc": f"断点续传（从消息 {r.get('last_synced_msg_id')} 之后继续）",
            }
        elif choice == "id":
            wiz.goto("from_id")
            await self._render_step(event, wiz)
            return
        elif choice == "time":
            wiz.goto("from_time")
            await self._render_step(event, wiz)
            return
        wiz.goto(wiz.next_step() or "confirm")
        await self._render_step(event, wiz)

    async def _on_target_choice(self, event, choice: str):
        wiz = self._get(event)
        if not wiz:
            return
        if choice == "here":
            topic_id = get_target_topic_id(event)
            title = await self._format_display(
                event.chat_id, topic_id, prefer_bot=True)
            wiz.data["target"] = {
                "chat_id": event.chat_id,
                "topic_id": topic_id,
                "title": title,
            }
            await self._annotate_resumable(wiz)
            wiz.goto(wiz.next_step() or "confirm")
            await self._render_step(event, wiz)

    async def _annotate_resumable(self, wiz: Wizard) -> None:
        """源与目标都确定后，查一下有没有可续传的同源同目标任务。"""
        if wiz.kind != "clone":
            return
        src = wiz.data.get("source") or {}
        tgt = wiz.data.get("target") or {}
        if not src.get("chat_id") or not tgt.get("chat_id"):
            return
        try:
            found = await models.find_resumable_clone(
                self.db, src["chat_id"], tgt["chat_id"],
                src.get("topic_id"), tgt.get("topic_id"))
        except Exception as e:
            logger.debug("查询可续传任务失败: %s", e)
            found = None
        if found:
            src["resumable"] = found
        else:
            src.pop("resumable", None)
        wiz.data["source"] = src

    async def _on_text(self, event, wiz: Wizard):
        raw = (event.raw_text or "").strip()

        if wiz.step == "src":
            info, err = await self._resolve_source(event, raw)
            if not info:
                await event.reply(f"❌ {err or '无法解析该源'}\n请检查链接或 @用户名 后重试。")
                return
            wiz.data["source"] = info
            wiz.goto(wiz.next_step() or "confirm")
            await self._render_step(event, wiz)
            return

        if wiz.step == "from_id":
            if not raw.isdigit():
                await event.reply("❌ 请发送纯数字的消息 ID。")
                return
            msg_id = int(raw)
            wiz.data["start"] = {
                "kind": "id", "msg_id": msg_id,
                "desc": f"从指定消息 {msg_id} 开始",
            }
            wiz.goto(wiz.next_step() or "confirm")
            await self._render_step(event, wiz)
            return

        if wiz.step == "from_time":
            when = parse_start_time(raw)
            if not when:
                await event.reply("❌ 时间格式无法识别，试试 `2024-01-31` 或 `2024-01-31 12:30`。")
                return
            wiz.data["start"] = {
                "kind": "time", "date": when.isoformat(),
                "desc": f"从 {when.strftime('%Y-%m-%d %H:%M')} 之后开始",
            }
            wiz.goto(wiz.next_step() or "confirm")
            await self._render_step(event, wiz)
            return

        if wiz.step == "target":
            tgt, err = await self._resolve_target(event, raw)
            if not tgt:
                await event.reply(f"❌ {err or '无法解析目标频道'}")
                return
            wiz.data["target"] = tgt
            await self._annotate_resumable(wiz)
            wiz.goto(wiz.next_step() or "confirm")
            await self._render_step(event, wiz)
            return

        await event.reply("⚠️ 当前步骤不需要文字输入，请点上面的按钮操作。")

    async def _on_confirm(self, event):
        wiz = self._get(event)
        if not wiz:
            return
        src = wiz.data.get("source") or {}
        tgt = wiz.data.get("target") or {}
        start = wiz.data.get("start") or {}
        if not src or not tgt:
            await event.answer("信息不完整，请重新开始", alert=True)
            return

        payload = dict(
            kind=wiz.kind,
            source_chat_id=src["chat_id"],
            source_topic_id=src.get("topic_id"),
            source_title=src.get("title"),
            target_chat_id=tgt["chat_id"],
            target_topic_id=tgt.get("topic_id"),
            target_title=tgt.get("title"),
            mode=wiz.data.get("mode") or "copy",
            start=start,
            userbot=src.get("userbot"),
            chat_id=event.chat_id,
            sender_id=event.sender_id,
        )
        task_id, err = await self._create_task(payload, event)
        if not task_id:
            # 不光弹 toast，还要在会话里留一条可见的失败原因（toast 很容易被忽略）
            await event.answer(err or "创建失败", alert=True)
            text = (
                "❌ 创建任务失败\n\n"
                f"原因: {err or '未知错误'}\n\n"
                "常见原因：\n"
                "• UserBot 还没加入源频道（监听必须有读权限）\n"
                "• Bot 不是目标频道管理员，无法发送\n"
                "• 链接失效或频道已转为私有"
            )
            try:
                await event.edit(text, buttons=[
                    [Button.inline("🔁 重试", data="m:go")],
                    [Button.inline("« 主菜单", data="m:home")]])
            except Exception:
                await event.respond(text)
            logger.warning("菜单创建任务失败 kind=%s: %s", wiz.kind, err)
            return

        # 已存在相同的监控任务：不重复创建，直接把已有任务展示出来
        if err == "exists":
            self.drop(event.chat_id, event.sender_id)
            existing = await models.get_task(self.db, task_id) or {}
            status = existing.get("status", "running")
            await event.answer("该监控任务已存在，未重复创建", alert=True)
            text = (f"ℹ️ 相同的监控任务已存在：#{task_id}（{status}）\n"
                    "未重复创建，避免同一条消息被转发两次。")
            buttons = []
            if status in ("paused", "failed"):
                text += "\n需要继续接收新消息，请点「继续监控」。"
                buttons.append([Button.inline("▶️ 继续监控", data=f"m:resume:{task_id}")])
            buttons.append([Button.inline("📋 查看详情", data=f"m:task:{task_id}")])
            buttons.append([Button.inline("📡 全部监控", data="m:monitors"),
                            Button.inline("« 主菜单", data="m:home")])
            try:
                await event.edit(text, buttons=buttons)
            except Exception:
                pass
            return

        self.drop(event.chat_id, event.sender_id)
        await event.answer(f"任务 #{task_id} 已创建")
        # 启动/确认消息由 create_task 回调发出（克隆需要消息 ID 来实时刷新进度），
        # 这里只把向导消息收尾，避免留下一个可点的旧键盘。
        done_text = (f"✅ 监控任务 #{task_id} 已创建，开始监听新消息。"
                     if wiz.kind == "monitor" else
                     f"✅ 任务 #{task_id} 已启动，进度见下方消息。")
        try:
            await event.edit(
                done_text,
                buttons=[[Button.inline("📋 查看详情", data=f"m:task:{task_id}")],
                         [Button.inline("« 主菜单", data="m:home")]])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def show_main_menu(self, event):
        """给 /start、/menu 使用；也用于任务完成后的回跳。"""
        self.drop(event.chat_id, event.sender_id)
        text, buttons = self._main_menu()
        await event.respond(text, buttons=buttons)

    async def start_wizard_via_command(self, event, kind: str = "clone",
                                       link: str | None = None):
        """/clone、/monitor 命令降级入口：直接进入向导。"""
        wiz = self._start_wizard(event, kind)
        if link:
            info, err = await self._resolve_source(event, link)
            if info:
                wiz.data["source"] = info
                wiz.goto(wiz.next_step() or "confirm")
        await self._render_step(event, wiz, edit=False)
