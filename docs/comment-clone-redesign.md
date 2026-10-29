# 评论区克隆改造方案（触发词泛化 + 自动进讨论组 + 完整克隆）

> 目标：把"只有正文写了 `评论区获取` 才去搬评论区"的窄口径，改成"正文出现 `评论区` 就去搬"的宽口径；
> 命中后由 UserBot **定位并进入对应讨论组**，把该帖评论区**完整、按时间正序、幂等**地克隆到目标；
> 同时保留 `all`（全量连带）与 `off` 两种既有模式，行为不回退。
>
> **状态：已实现**（见 CHANGELOG `[Unreleased]`）。落地时的两处偏差：
> ① `comment_as_reply`（评论挂到目标帖下方）未纳入本期，留作后续增强；
> ② 测试桩按真实 `messages.getReplies`（id 倒序）改写，确保"正序落地"的断言真的有效。

---

## 一、现状分析：业务逻辑与评论区链路

### 1.1 模块地图（与本需求相关的部分）

| 层 | 文件 | 职责 |
|---|---|---|
| 入口 | `main.py` / `bot/handlers.py` | 建任务、起 `start_sync`、`CloneGate` 并发闸门（`bot/handlers.py:258-259`） |
| 克隆主循环 | `core/syncer.py` | 流式分批 → 转发单元 → 断点续传 → 进度/看门狗 |
| 受限源克隆 | `core/restricted_syncer.py` | Takeout 通道另跑一套（评论**未覆盖**） |
| 转发引擎 | `core/forwarder.py` | 四级降级策略链（Bot → UserBot 转发 → UserBot 读 Bot 写 → 下载上传） |
| 判定纯逻辑 | `core/message_logic.py` | 相册成组、硬封禁判定 |
| 数据 | `db/database.py` / `db/models.py` | `tasks` / `message_map` / `failed_messages` |
| 配置 | `config.example.yaml:96-105` | `clone.comments_mode / comment_hint / comment_max` |

### 1.2 评论区钩子的位置

`core/syncer.py:729-745`，位于「一个转发单元（单条 / 相册）处理完」之后、下一个单元之前：

```
批次(batch_size=200)
 └─ build_forward_units(batch)            # core/message_logic.py
     └─ for unit in units:
         ├─ 受限? → _forward_restricted_unit(Takeout)
         ├─ 否则  → _forward_unit(四级降级)
         └─ if comments_mode != "off":    # ← 评论区钩子（本需求主战场）
              for sid in source_ids:
                  _clone_post_comments(...)   # core/syncer.py:380
```

因此评论是天生的「串行附带动作」：帖子落地 → 搬评论区 → 再走下一条。这也意味着**评论区跑得越久，主游标越久不动**（靠 `state["beat"]` 与看门狗兜底，`core/syncer.py:455-481`）。

### 1.3 现有 `_clone_post_comments` 逐行问题清单

`core/syncer.py:380-440`，改前行为：

```python
if self.comments_mode == "off" or userbot is None: return 0, 0
if self.comments_mode == "hint":
    if self.comment_hint not in text: return 0, 0        # ← ① 单关键词、精确子串
if not replies or not replies.comments: return 0, 0      # ← ② 只判有无，不看条数
disc_id = get_peer_id(PeerChannel(replies.discussion_id)) # ← ③ 解析了却几乎没用
async for c in userbot.iter_messages(source_chat_id, reply_to=post.id):  # ← ④ 走频道侧
    ...
    chat_for = disc_id or c.chat_id or source_chat_id
    forward_message(chat_for, c.id, ...)                  # ← ⑤ 失败标记被当成功
    # ← ⑥ 无正序、无去重、无进组、无心跳
```

| # | 问题 | 影响 |
|---|---|---|
| ① | 触发词写死为完整串 `评论区获取` | 只写「看评论区」「评论区自取」「👇评论区」的帖子全部漏搬，正是最常见的反爬写法 |
| ② | 只判 `replies.comments` 布尔位 | 0 条评论的帖子也会白跑一次进组逻辑 |
| ③ | `discussion_id` 解析后仅作 `chat_for` 兜底 | 从未真正"进入讨论组"；频道侧 `getReplies` 在读不到时会静默返回空 |
| ④ | 只走 `iter_messages(source_chat_id, reply_to=post.id)` | 依赖 `messages.getReplies(peer=频道)`；私有讨论组 / 频道受限时读不到，**静默少搬**（异常被 `except` 吞成 warning） |
| ⑤ | `forward_message` 全策略失败会 `send_fail_marker` 并返回消息 ID（`core/forwarder.py:401-410`、`1167-1186`） | 失败被计成"成功克隆了 N 条评论"，汇报数字虚高，目标里留下 `#fail2forward` 垃圾 |
| ⑥ | 无排序、无幂等、无入组、无心跳 | 目标频道评论**倒序**显示；`/kljx` 重跑会重复搬；长评论区触发看门狗误报 |

另有两处**流程级**缺陷：
- 帖子本身转发失败时仍会去搬它的评论（钩子只看 `comments_mode`，不看 `target_ids`）→ 目标里出现"无主评论"。
- `retry_failed`（`core/syncer.py:487-536`）补发帖子不会补评论；`monitor`（实时监听，`core/monitor.py`）完全不搬评论。后者属既有设计边界，本次不改，只在文档标注。

---

## 二、设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 触发词 | 默认 `("评论区",)`，支持**多关键词列表** | 「评论区获取」是「评论区」的超集 → 旧配置零成本向后兼容；多关键词覆盖「看评论区/评论区自取」 |
| 匹配方式 | 归一化后子串匹配（NFKC + 去零宽/空白） | 反爬频道常写「评 论 区」「评论区\u200b」，裸 `in` 会漏 |
| 讨论组定位 | `replies.discussion_id` → 回退 `GetDiscussionMessageRequest` → 回退 `GetFullChannel.linked_chat_id` | `getDiscussionMessage` 是**唯一**能拿到「频道帖子 ID → 组内顶层消息 ID」的服务端接口 |
| 读取通道 | **组侧优先**，失败回落频道侧 `getReplies` | 组侧只要在组内就能读（私有组也 OK）；频道侧零成本，作兜底 |
| 进组 | `comment_join: auto`（仅 hint 命中帖自动进组）/ `always` / `never` | `all` 模式下无差别入群风控风险过高，默认不做 |
| 完整性 | `comment_max: 0` 表示不限；`comment_order: asc` 正序 | "完整克隆"要能真搬完，同时保留安全阀 |
| 幂等 | 新增 `comment_threads`（线程级）+ `comment_map`（评论级） | 续跑 / `/kljx` 重跑不重复投递，和仓库现有"幂等去重"承诺对齐 |
| 失败语义 | 读取失败单独计数 `read_failed`，不混入 `fail` | 读不到和转不出去是两种故障，运维处置不同 |

---

## 三、修改方案

### 3.1 配置项（`config.example.yaml:96-105`）

改前：

```yaml
  comments_mode: hint
  comment_hint: "评论区获取"   # 命中即克隆
  comment_max: 1000
```

改后（`comment_hint` 保留为兼容键，新代码优先读 `comment_hints`）：

```yaml
  # 评论区克隆：帖子落地后再把该帖评论区搬到目标频道。
  # hint = 帖子文本含 comment_hints 中任一关键词才触发（默认「评论区」）；
  # all  = 凡开启评论区的帖子都触发（全量连带，请求频率显著上升）；
  # off  = 禁用。
  comments_mode: hint
  comment_hints: ["评论区"]         # 任一命中即触发；"评论区获取" 是它的超集，旧库无需改
  comment_hint: ""                  # 兼容旧键：comment_hints 缺失且此处非空时生效
  comment_max: 1000                 # 单帖上限，0 = 不限（完整克隆）
  comment_scan_max: 20000           # comment_max=0 时的硬扫描上限，防异常巨型线程
  comment_order: asc                # asc=按时间正序（默认，阅读体验对）/ desc=限条数时取最新
  comment_read_from: auto           # auto=组侧优先回落频道侧 / group / channel
  comment_join: auto                # auto=仅 hint 命中帖自动进组 / always / never
  comment_min_replies: 0            # >0 时评论数低于该值的帖子直接跳过
  comment_dedupe: true              # 线程级幂等：已搬过的帖子不再搬
  comment_pause_every: 200          # 每搬 N 条评论停一下
  comment_pause: 1.0                # 停顿时长（秒）
  comment_as_reply: false           # true=评论挂到目标帖下方（需 message_map 命中）
```

零配置迁移：旧库只有 `comment_hint: "评论区获取"` → `parse_hints` 取该值 → 与「评论区」行为等价（因为前者包含后者）。

### 3.2 新增 `core/comment_trigger.py`（纯逻辑，可离线单测）

```python
"""评论区触发判定：把「这条帖子要不要搬评论区」抽成纯函数。

历史实现要求正文包含完整提示词「评论区获取」，只写「评论区」「看评论区」的
帖子全部漏搬；这里统一做归一化 + 多关键词匹配，并兼容旧的单字符串配置。
"""
from __future__ import annotations

import re
import unicodedata

DEFAULT_HINTS: tuple[str, ...] = ("评论区",)

# 零宽 / BOM / 软连字符：反爬频道常把它们塞进关键词中间
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """全角→半角、去掉零宽字符与所有空白，让「评 论 区」也能命中。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).translate(_INVISIBLE)
    return _WS_RE.sub("", s)


def parse_hints(raw_hints, raw_hint) -> tuple[str, ...]:
    """优先 comment_hints（列表或逗号串），否则回落到旧键 comment_hint。

    旧实现把 comment_hint 设为空串等价于「关闭」，这是隐式语义、极易误配；
    新实现里空值统一回到默认值，要关请用 comments_mode: off。
    """
    items: list[str] = []
    if isinstance(raw_hints, (list, tuple)):
        items = [str(x) for x in raw_hints]
    elif isinstance(raw_hints, str) and raw_hints.strip():
        items = re.split(r"[,，|]", raw_hints)
    if not items and isinstance(raw_hint, str) and raw_hint.strip():
        items = [raw_hint]

    cleaned: list[str] = []
    for it in items:
        n = normalize_text(it)
        if n and n not in cleaned:
            cleaned.append(n)
    return tuple(cleaned) or DEFAULT_HINTS


def match_comment_hint(text: str | None, hints: tuple[str, ...]) -> str | None:
    """命中返回关键词（写日志/汇报用），否则 None。"""
    norm = normalize_text(text)
    if not norm:
        return None
    for h in hints:
        if h in norm:
            return h
    return None


def should_clone_comments(post, *, mode: str, hints: tuple[str, ...],
                          min_replies: int = 0) -> tuple[bool, str]:
    """统一判定，返回 (是否搬, 原因)。原因会进日志与汇报，便于排查漏搬。

    原因取值：off / no-comments / too-few(N) / all / hint:<关键词> / no-hint
    """
    if mode == "off":
        return False, "off"

    replies = getattr(post, "replies", None)
    if not replies or not getattr(replies, "comments", False):
        return False, "no-comments"

    count = int(getattr(replies, "replies", 0) or 0)
    if min_replies > 0 and count < min_replies:
        return False, f"too-few({count})"

    if mode == "all":                      # 全量连带模式：只见评论区就搬
        return True, "all"

    text = getattr(post, "text", None) or getattr(post, "message", "") or ""
    hit = match_comment_hint(text, hints)
    return (True, f"hint:{hit}") if hit else (False, "no-hint")
```

### 3.3 新增 `core/discussion.py`（定位 + 进组）

```python
"""定位帖子的讨论组（评论区）并保证 UserBot 可读。

「频道帖子 ID → 组内顶层消息 ID」的映射只有服务端知道，对应
messages.getDiscussionMessage；拿到映射后就能直接在组内按 top_msg_id 翻页：
- 私有讨论组只要 UserBot 在组内就能读（不依赖频道的 getReplies 权限）；
- 读到的 message.id 就是组内 ID，转发时不必再猜 chat。
"""
from __future__ import annotations

import logging

from telethon import errors, functions, utils
from telethon.tl.types import Channel, PeerChannel

logger = logging.getLogger("tg_forward_bot.discussion")


def peer_id_of(channel_id: int | None) -> int:
    """裸 channel_id → -100 前缀 peer id（失败返回 0）。"""
    try:
        return utils.get_peer_id(PeerChannel(int(channel_id)))
    except Exception:
        return 0


async def resolve_discussion(client, source_chat_id: int, post,
                             cache: dict[int, int]) -> tuple[int, int | None]:
    """返回 (讨论组 chat_id, 组内顶层消息 ID)；无法定位时 (0, None)。

    cache 以源频道为键缓存讨论组 ID（频道级信息，跨帖子复用），
    组内顶层 ID 是帖子级信息，无法缓存，每帖一次 RPC。
    """
    disc_id = cache.get(source_chat_id)
    if disc_id is None:
        did = getattr(getattr(post, "replies", None), "discussion_id", None)
        disc_id = peer_id_of(did) if did else 0
        if not disc_id:
            disc_id = await _linked_chat_id(client, source_chat_id)
        cache[source_chat_id] = disc_id

    if not disc_id:
        return 0, None
    return disc_id, await _discussion_top_id(client, source_chat_id, post.id)


async def _linked_chat_id(client, source_chat_id: int) -> int:
    """兜底：从频道全量信息里取 linked_chat_id（顶帖可能没带 replies）。"""
    try:
        entity = await client.get_entity(source_chat_id)
        if not isinstance(entity, Channel):
            return 0
        full = await client(functions.channels.GetFullChannelRequest(entity))
        linked = getattr(full.full_chat, "linked_chat_id", None)
        return peer_id_of(linked) if linked else 0
    except Exception as e:
        logger.debug("解析频道 %s 关联讨论组失败: %s", source_chat_id, e)
        return 0


async def _discussion_top_id(client, source_chat_id: int, post_id: int) -> int | None:
    """频道帖子 → 讨论组里的顶层消息 ID；返回 None 表示该帖无讨论串。"""
    try:
        r = await client(functions.messages.GetDiscussionMessageRequest(
            peer=source_chat_id, msg_id=int(post_id)))
    except Exception as e:
        logger.debug("取帖子 %s 的讨论消息失败: %s", post_id, e)
        return None
    msgs = getattr(r, "messages", None) or []
    if not msgs:
        return None
    return min(int(m.id) for m in msgs)      # 同 Telethon _get_comment_data 的取法


async def ensure_member(client, disc_id: int, cache: set[int], *, join: bool) -> bool:
    """确保 UserBot 在讨论组里；返回"是否可读"。可读结果按讨论组缓存。"""
    if not disc_id:
        return False
    if disc_id in cache:
        return True

    try:
        await client(functions.channels.GetParticipantRequest(disc_id, "me"))
        cache.add(disc_id)
        return True
    except errors.UserNotParticipantError:
        pass
    except Exception as e:                   # 组被删 / 被封 / 无权限
        logger.info("讨论组 %s 不可读: %s: %s", disc_id, type(e).__name__, e)
        return False

    if not join:
        return False
    try:
        await client(functions.channels.JoinChannelRequest(disc_id))
        logger.info("UserBot 已自动加入讨论组 %s（评论区克隆）", disc_id)
    except errors.UserAlreadyParticipantError:
        pass
    except errors.InviteRequestSentError:
        logger.info("讨论组 %s 需管理员审批，本次先按频道侧读取", disc_id)
        return False
    except Exception as e:
        logger.warning("加入讨论组 %s 失败: %s: %s", disc_id, type(e).__name__, e)
        return False

    cache.add(disc_id)
    return True
```

### 3.4 重写 `core/syncer.py:380-440`

```python
    async def _clone_post_comments(
        self, task_id: int, post, source_chat_id: int, target_chat_id: int,
        mode: str, target_topic_id: int | None,
        userbot: TelegramClient | None, *, tick=None,
    ) -> tuple[int, int, bool]:
        """克隆一条帖子的整条评论区，返回 (成功, 转发失败, 是否读取失败)。

        触发判定见 core/comment_trigger.should_clone_comments：
        - hint（默认）：正文含 comment_hints 任一关键词（默认「评论区」）
        - all ：凡开启评论区的帖子都搬（全量连带）
        - off ：禁用
        读取优先走讨论组（必要时自动入组），失败回落频道侧 getReplies。
        """
        if self.comments_mode == "off" or userbot is None:
            return 0, 0, False

        hit, reason = should_clone_comments(
            post, mode=self.comments_mode, hints=self.comment_hints,
            min_replies=self.comment_min_replies)
        if not hit:
            logger.debug("帖子 %s 不搬评论区（%s）", getattr(post, "id", "?"), reason)
            return 0, 0, False

        if self.comment_dedupe and await models.is_comment_thread_done(
                self.db, task_id, post.id):
            logger.info("帖子 %s 评论区此前已克隆，跳过（幂等）", post.id)
            return 0, 0, False

        disc_id, top_id = await resolve_discussion(
            userbot, source_chat_id, post, self._disc_cache)

        # hint 命中才自动入组：all 模式无差别入群，风控代价太高
        join = self.comment_join == "always" or (
            self.comment_join == "auto" and reason.startswith("hint"))
        readable = await ensure_member(
            userbot, disc_id, self._disc_member, join=join)

        # auto：组侧优先（私有组也读得到），频道侧兜底（零额外 RPC）
        attempts: list[tuple[int, int, str]] = []
        if readable and top_id and self.comment_read_from in ("auto", "group"):
            attempts.append((disc_id, top_id, "group"))
        if self.comment_read_from in ("auto", "channel") or not attempts:
            attempts.append((source_chat_id, post.id, "channel"))

        ok = fail = 0
        read_failed = False
        used = "-"
        for chat_for, thread_id, where in attempts:
            ok, fail, err = await self._drain_comment_thread(
                task_id, post, chat_for, thread_id, target_chat_id,
                mode, target_topic_id, userbot, where=where, tick=tick)
            if err:
                read_failed = True
                continue                     # 上一个通道读不到 → 换通道再试
            used = where
            break

        logger.info("帖子 %s 评论区: 触发(%s) 通道=%s 成功=%d 失败=%d 读失败=%s",
                    post.id, reason, used, ok, fail, read_failed)
        if self.comment_dedupe and (ok or fail):
            await models.mark_comment_thread(
                self.db, task_id, post.id, disc_id,
                ok=ok, fail=fail, done=not fail)
        return ok, fail, read_failed

    async def _drain_comment_thread(
        self, task_id: int, post, chat_id: int, thread_id: int,
        target_chat_id: int, mode: str, target_topic_id: int | None,
        userbot: TelegramClient, *, where: str, tick=None,
    ) -> tuple[int, int, bool]:
        """翻完一条评论线程并逐条转发，返回 (成功, 失败, 读取是否异常)。"""
        cap = self.comment_max or 0
        scan_cap = cap or self.comment_scan_max
        collected: list = []
        try:
            async for c in userbot.iter_messages(chat_id, reply_to=thread_id):
                if self._cancel_flags.get(task_id) or len(collected) >= scan_cap:
                    break
                if isinstance(c, MessageService):
                    continue
                if not getattr(c, "text", None) and getattr(c, "media", None) is None:
                    continue
                collected.append(c)
        except Exception as e:
            logger.warning("读取帖子 %s 评论区失败（%s, chat=%s）: %s: %s",
                           post.id, where, chat_id, type(e).__name__, e)
            return 0, 0, True

        if not collected:
            return 0, 0, False

        # 服务端按 id 倒序返回；正序搬，目标频道读起来才是对话顺序
        if self.comment_order == "asc":
            collected.reverse()
        if cap:
            collected = collected[:cap]      # asc 时保留最早 cap 条，desc 时保留最新 cap 条

        ok = fail = 0
        for idx, c in enumerate(collected, 1):
            if self._cancel_flags.get(task_id):
                break
            chat_for = int(getattr(c, "chat_id", 0) or 0) or chat_id
            try:
                target_id = await self.forwarder.forward_message(
                    chat_for, c.id, target_chat_id, mode, target_topic_id,
                    caller="syncer-comment", userbot=userbot,
                    with_fail_marker=False)   # 评论失败不往目标灌 #fail2forward
            except Exception as e:
                logger.warning("评论 %s/%s 克隆失败: %s", chat_for, c.id, e)
                target_id = None
            if target_id:
                ok += 1
                await models.save_comment_map(
                    self.db, task_id, post.id, c.id, target_id)
            else:
                fail += 1
            if tick is not None:
                tick()                        # 喂看门狗心跳，长评论区不误报
            if self.comment_pause_every and idx % self.comment_pause_every == 0:
                await asyncio.sleep(self.comment_pause)
        return ok, fail, False
```

`__init__` 追加（`core/syncer.py:82-88` 之后）：

```python
        self.comments_mode = str(clone_cfg.get("comments_mode") or "hint").lower()
        if self.comments_mode not in ("hint", "all", "off"):
            self.comments_mode = "hint"
        self.comment_hints = parse_hints(
            clone_cfg.get("comment_hints"), clone_cfg.get("comment_hint"))
        self.comment_max = max(0, int(clone_cfg.get("comment_max", 1000) or 0))
        self.comment_scan_max = max(100, int(clone_cfg.get("comment_scan_max") or 20000))
        self.comment_order = str(clone_cfg.get("comment_order") or "asc").lower()
        self.comment_read_from = str(clone_cfg.get("comment_read_from") or "auto").lower()
        self.comment_join = str(clone_cfg.get("comment_join") or "auto").lower()
        self.comment_min_replies = max(0, int(clone_cfg.get("comment_min_replies") or 0))
        self.comment_dedupe = bool(clone_cfg.get("comment_dedupe", True))
        self.comment_pause_every = max(0, int(clone_cfg.get("comment_pause_every") or 0))
        self.comment_pause = max(0.0, float(clone_cfg.get("comment_pause") or 1.0))
        self._disc_cache: dict[int, int] = {}     # 源频道 → 讨论组 ID
        self._disc_member: set[int] = set()       # 已确认可读的讨论组
```

### 3.5 主循环钩子改造（`core/syncer.py:729-745`）

```python
                            # 评论区钩子：帖子本身落地后才搬评论，避免"无主评论"
                            if self.comments_mode != "off" and (target_ids or dup_count):
                                for sid in source_ids:
                                    post = msg_by_id.get(sid)
                                    if post is None:
                                        continue
                                    c_ok, c_fail, c_err = await self._clone_post_comments(
                                        task_id, post, source_chat_id,
                                        target_chat_id, mode, target_topic_id,
                                        active_userbot,
                                        tick=lambda: state.update(
                                            beat=time.monotonic()))
                                    if c_ok or c_fail:
                                        comments_done += c_ok
                                        comments_fail += c_fail
                                        comments_posts += 1
                                        state["comment_hint"] = f"msg {sid}"
                                    if c_err:
                                        comments_read_fail += 1
```

配套：`core/syncer.py:600` 增加计数器 `comments_read_fail = 0`；`_render()`（`608-621`）追加一行

```python
                f"评论区: {comments_posts} 帖 / {comments_done:,} 条"
                + (f" · 读取失败 {comments_read_fail} 帖" if comments_read_fail else "")
```

### 3.6 完成汇报（`core/syncer.py:817-822`）

```python
        comment_line = ""
        if comments_posts or comments_read_fail:
            comment_line = (f"\n• 评论区: {comments_posts} 帖克隆了 "
                            f"{comments_done:,} 条")
            if comments_fail:
                comment_line += f"，转发失败 {comments_fail} 条"
            if comments_read_fail:
                comment_line += f"，读取失败 {comments_read_fail} 帖"
```

### 3.7 幂等：DB + models

`db/database.py:105-134` 的 `_create_tables` 追加两张表（`CREATE TABLE IF NOT EXISTS`，老库自动补）：

```sql
CREATE TABLE IF NOT EXISTS comment_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    source_post_id INTEGER NOT NULL,
    disc_id INTEGER DEFAULT 0,
    ok_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, source_post_id)
);
CREATE TABLE IF NOT EXISTS comment_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    source_post_id INTEGER NOT NULL,
    source_comment_id INTEGER NOT NULL,
    target_msg_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, source_post_id, source_comment_id)
);
```

> 为什么不复用 `message_map`：它的主键语义是 `(task_id, source_msg_id)`，而评论 id 与帖子 id 在各自 chat 内独立编号，混存会互相覆盖 / 触发唯一约束误判。

`db/models.py` 追加（风格对齐 `306-360`）：

```python
async def is_comment_thread_done(db: Database, task_id: int, post_id: int) -> bool:
    cursor = await db.db.execute(
        "SELECT finished FROM comment_threads WHERE task_id=? AND source_post_id=?",
        (task_id, int(post_id)))
    row = await cursor.fetchone()
    return bool(row and int(row["finished"]))


async def mark_comment_thread(db: Database, task_id: int, post_id: int,
                              disc_id: int = 0, *, ok: int = 0, fail: int = 0,
                              done: bool = False) -> None:
    await db.db.execute(
        """INSERT INTO comment_threads
             (task_id, source_post_id, disc_id, ok_count, fail_count, finished)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(task_id, source_post_id) DO UPDATE SET
             disc_id=excluded.disc_id,
             ok_count=ok_count+excluded.ok_count,
             fail_count=fail_count+excluded.fail_count,
             finished=MAX(finished, excluded.finished),
             updated_at=CURRENT_TIMESTAMP""",
        (task_id, int(post_id), int(disc_id or 0), int(ok), int(fail), 1 if done else 0))
    await db.db.commit()


async def save_comment_map(db: Database, task_id: int, post_id: int,
                           comment_id: int, target_msg_id: int) -> None:
    await db.db.execute(
        "INSERT OR IGNORE INTO comment_map"
        " (task_id, source_post_id, source_comment_id, target_msg_id) VALUES (?,?,?,?)",
        (task_id, int(post_id), int(comment_id), int(target_msg_id)))
    await db.db.commit()


async def count_comment_map(db: Database, task_id: int) -> int:
    """已落地的评论条数（续跑后汇报用，不再依赖内存计数）。"""
    cursor = await db.db.execute(
        "SELECT COUNT(*) AS n FROM comment_map WHERE task_id=?", (task_id,))
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
```

### 3.8 修掉"失败标记算成功"（`core/forwarder.py:385-410`）

```python
    async def forward_message(self, source_chat_id: int, msg_id: int,
                              target_chat_id: int, mode: str = "copy",
                              target_topic_id: int | None = None,
                              caller: str = "monitor",
                              userbot: TelegramClient | None = None,
                              with_fail_marker: bool = True) -> int | None:
        ...
        if not with_fail_marker:            # 评论等"附带内容"不灌失败标记
            return None
        logger.warning("msg=%s 所有策略失败，发送 #fail2forward 标记", msg_id)
```

---

## 四、关键代码位置清单

| # | 文件:行号 | 现状 | 改法 |
|---|---|---|---|
| 1 | `core/syncer.py:82-88` | 解析 `comments_mode / comment_hint / comment_max` | 改读 `comment_hints` 等 8 个新键，选 `parse_hints`，新增 `_disc_cache/_disc_member` |
| 2 | `core/syncer.py:380-440` | `_clone_post_comments` 整体 | 用 3.4 版本替换；拆出 `_drain_comment_thread` |
| 3 | `core/syncer.py:394-397` | `if self.comment_hint not in text` | 换 `should_clone_comments()`（多关键词 + 归一化 + 条数门槛） |
| 4 | `core/syncer.py:399-413` | `replies` 判定 + `discussion_id` 只算不用 | 交 `resolve_discussion()` + `ensure_member()`，真正进组 |
| 5 | `core/syncer.py:417-430` | 只走频道侧、无排序、无去重 | 组侧→频道侧双通道；`comment_order` 正序；`comment_map` 去重 |
| 6 | `core/syncer.py:600` | `comments_done/fail/posts` | 增 `comments_read_fail` |
| 7 | `core/syncer.py:608-621` | `_render()` 无评论进度 | 追加评论区一行（帖数/条数/读取失败） |
| 8 | `core/syncer.py:729-745` | 钩子不看 `target_ids` | 加 `(target_ids or dup_count)` 守卫；传 `tick` 喂心跳；计 `read_fail` |
| 9 | `core/syncer.py:817-822` | 汇报两段式 | 增加"转发失败 / 读取失败"分项 |
| 10 | `core/forwarder.py:385-410` | 失败也返回标记消息 ID | 新增 `with_fail_marker` 参数，评论传 `False` |
| 11 | `bot/link_parser.py:125-140` | 已有 `resolve_linked_chat` | 逻辑迁入 `core/discussion.py:_linked_chat_id`，原函数保留供解析器用 |
| 12 | `db/database.py:105-134` | 三张表 | 追加 `comment_threads` / `comment_map` |
| 13 | `db/models.py:306-360` 附近 | `message_map` 系列 | 追加 4 个评论相关函数 |
| 14 | 新建 `core/comment_trigger.py` | — | 触发判定纯逻辑（3.2） |
| 15 | 新建 `core/discussion.py` | — | 讨论组定位与入组（3.3） |
| 16 | `config.example.yaml:96-105` | 3 个键 | 换成 3.1 的 12 个键 |
| 17 | `docs/clone.md:360-378`、`README.md:16`、`README.en.md:16`、`CHANGELOG.md:48-54` | 文案写「评论区获取」 | 改为「评论区」口径 + 新配置说明 |
| 18 | `test/test_comments_clone.py:126-264` | 6 个用例 | 保留并新增 4 个用例（见 §6.2） |

---

## 五、调整后行为说明

### 5.1 触发判定矩阵

| 帖子文本 | `replies.comments` | 评论数 | `hint`（新默认） | `all` | `off` |
|---|---|---|---|---|---|
| 含「评论区」 | true | ≥1 | **触发** | 触发 | 不触发 |
| 含「评论区获取」 | true | ≥1 | **触发**（子串包含） | 触发 | 不触发 |
| 含「评 论 区」/零宽夹杂 | true | ≥1 | **触发**（归一化后匹配） | 触发 | 不触发 |
| 含「看评论区自取」 | true | ≥1 | **触发** | 触发 | 不触发 |
| 无关键词 | true | ≥1 | 不触发（`hint:no-hint`） | 触发 | 不触发 |
| 任意 | true | 0 且 `comment_min_replies>0` | 跳过（`too-few(0)`） | 跳过 | 不触发 |
| 任意 | false（未开评论区） | — | 跳过（`no-comments`），只记 debug 日志，不计失败 | 同左 | 不触发 |
| 帖子本身转发失败 | — | — | **不再搬评论**（钩子守卫） | 同左 | 不触发 |

### 5.2 单帖时序（改后）

```
帖子落地成功（target_ids 非空）
  ├─ should_clone_comments()        → 纯函数判定，无网络请求
  ├─ 幂等检查 comment_threads       → 已搬过？直接跳过
  ├─ resolve_discussion()           → replies.discussion_id（0 RPC）
  │    └─ 缺失 → getDiscussionMessage（1 RPC）→ 组内 top_msg_id
  │    └─ 仍缺失 → getFullChannel.linked_chat_id（1 RPC，按频道缓存）
  ├─ ensure_member()                → 已在组内？缓存命中即返回
  │    └─ 不在 + 允许入组 → joinChannel（auto 仅 hint 命中帖）
  ├─ 组侧 iter_messages(disc, reply_to=top)  ── 成功 → 逐条转发（正序）
  │    └─ 读取异常 → 回落频道侧 iter_messages(频道, reply_to=post.id)
  ├─ 每条评论 forward_message(with_fail_marker=False) + save_comment_map
  ├─ 每 comment_pause_every 条 sleep(comment_pause)，每 tick 喂看门狗
  └─ mark_comment_thread(done=无失败)
```

### 5.3 计数口径

| 指标 | 口径 | 去向 |
|---|---|---|
| 帖子 `done_count` | **不含**评论，保持源频道口径 | 覆盖率分母不变，`source 1,000 / mapped 1,000 / 100%` 不会被评论污染 |
| `comments_done` | 评论成功落地条数 | 完成消息「评论区: N 帖克隆了 M 条」 |
| `comments_fail` | 评论转发全策略失败（不再含失败标记） | 「转发失败 X 条」 |
| `comments_read_fail` | 整个线程两条通道都读不出来 | 「读取失败 K 帖」→ 提示可用 `/kljx` 重跑该区间 |
| `comment_map` | 评论级落地映射 | 续跑汇报 + 幂等依据 |

### 5.4 风险与可观测性

- **量级放大**：触发词从 5 字收窄到 3 字，`hint` 模式命中面显著变大（"欢迎在评论区讨论"也会触发）。缓解：`comment_min_replies`、`comment_max`、`comment_pause_every`；`all` 模式风险不变（本就最激进）。
- **入组痕迹**：UserBot 会出现在讨论组成员列表里。`comment_join: auto` 只对关键词命中帖生效，`never` 可完全禁止（退化为只读公开组）。
- **RPC 成本**：每命中帖 +1 次 `getDiscussionMessage`，每新讨论组 +1 次 `getParticipant`/`joinChannel`（有缓存）。相比评论转发本身可忽略。
- **回滚**：`comments_mode: off` 一键关闭；`comment_hints: ["评论区获取"]` 可精确回到旧行为；DB 新表为 `CREATE IF NOT EXISTS`，不迁移、不删列，回滚旧镜像无影响。

---

## 六、兼容性与测试

### 6.1 向后兼容

| 项 | 结论 |
|---|---|
| 旧 `comment_hint: "评论区获取"` | `parse_hints` 读取 → 行为与旧版一致（略宽，因含「评论区」子串） |
| 旧 `comment_hint: ""`（隐含关闭） | 行为变化：现在回到默认「评论区」。要关闭请显式 `comments_mode: off`（配置注释已同步） |
| `comment_max: 1000` | 语义不变；新增 `0 = 不限` |
| 进行中的老任务 | 续跑时 `comment_threads` 为空 → 不做幂等拦截，与旧行为一致 |
| `retry_failed` / `monitor` | 本次不动（评论不在补发/实时监听范围内），文档标注为已知边界 |

### 6.2 测试

现有 `test/test_comments_clone.py` 6 个用例**预期仍通过**：
- `post(1, "干货合集 评论区获取 👇", has_comments=True)` → 新默认词「评论区」同样命中；
- `post(3, "闲聊帖", ...)` 不含关键词 → 仍不进；
- `all` / `off` / `comment_max` / 失败计数断言均保持。

需注意两处桩适配：`RecordingForwarder.forward_message` 要接受 `with_fail_marker` 形参；`post()` 若走 `comment_min_replies>0` 场景需补 `replies.replies` 字段。

新增用例：

1. `test_hint_matches_plain_comment_area` —— 正文只写「更多内容见评论区」→ 触发；
2. `test_hint_matches_with_zero_width` —— 「评&#8203;论&#8203;区」→ 触发；
3. `test_comments_forwarded_in_chronological_order` —— 断言调用序列为 5001→5003（改前是倒序）；
4. `test_fail_marker_not_counted_as_comment` —— 全策略失败时 `comments_done == 0` 且目标无标记消息；
5. `test_thread_skipped_when_post_failed` —— 帖子转发失败 → 不产生评论调用；
6. `test_dedupe_skips_done_thread` —— 第二次 `start_sync` 不重复搬同一帖；
7. `test_group_side_read_falls_back_to_channel` —— 组侧抛 `ChannelPrivateError` 时走频道侧并成功；
8. `test_join_only_for_hint_mode` —— `comment_join: auto` + `all` 模式 → 不调用 `joinChannel`。

---

## 七、落地顺序建议

1. `core/comment_trigger.py` + 单测（无网络依赖，先行）；
2. `core/discussion.py` + 单测（mock client）；
3. `db` 两张表 + models 函数；
4. `core/syncer.py` 重写方法 + 钩子 + 汇报；
5. `core/forwarder.py` 的 `with_fail_marker`；
6. 配置样例 / 文档 / CHANGELOG；
7. 灰度：先在单个频道把 `comment_min_replies: 3`、`comment_max: 200` 跑一轮，确认入组与顺序无误后再放开。
