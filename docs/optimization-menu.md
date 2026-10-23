# 七个项目的优化候选清单（决策版）

> 来源汇总：
> **A. 三个 Bot 框架** — node-telegram-bot-api / pyTelegramBotAPI / python-telegram-bot
> （详见 `docs/library-comparison.md`）
> **B. 三个同类项目** — mirror-leech-telegram-bot / tg-message-clone / KPSML-X
> （详见 `docs/project-comparison.md`）
> **C. 本轮新增** — [gaprj/TelegramChannelCloner](https://github.com/gaprj/TelegramChannelCloner)
> （18★，2026-03 创建，12 文件 / 894 行主程序，Telethon + Flask）
>
> 本文只做一件事：**把 7 个来源里所有值得拿的东西，列成可勾选的清单**，
> 每条标注收益、成本、风险、我的推荐度。你勾哪几条，我落地哪几条。

---

## 第 0 部分：TelegramChannelCloner 的「Bypasses All Restrictions」到底是什么

这是本轮的重点，所以先说结论再给证据。

### 0.1 拆解：它没有漏洞，只有路径选择

README 写「Bypasses All Restrictions - Clones media/text from private/restricted channels」，
听起来像有什么特殊手法。逐条对着源码看，实际是四件事的组合：

| README 的说法 | 实际实现 | 代码位置 |
| --- | --- | --- |
| 克隆私有/受限频道 | 用**用户账号**（不是 Bot）跑 `iter_messages`。用户号是频道成员就能读历史，Bot 不行 | `core_logic():834` |
| 绕过「禁止转发」(`noforwards`) | **完全不调用 `forward_messages`**，而是下载字节 → 用 `send_file` 重新上传成一条**新消息** | `download_message()` + `send_file()` |
| 绕过受限内容标记 | 自己拼 `InputMediaUploadedDocument`，副本是全新上传物，**不带原消息的任何 restriction 标记** | `preupload_large_video()` / `preupload_small_media()` |
| 处理「file reference expired」 | 每条消息下载前**重新 `get_messages(SOURCE, ids=...)`**，拿一个新的 file_reference | `download_message():365` |
| 抗封 | 全局 flood 计数驱动的递增退避 + 可选双账号 + 每条随机延迟 | `adaptive_flood_wait():204` |

**换句话说：`noforwards` 只拦截「转发」这个动作，不拦截「读取」和「重新上传」。**
所以所谓 bypass 的本质是——**换一条不复用原消息引用的路径**，而不是突破了什么校验。

其中唯一一个"真的能绕过"的东西值得单独指出：

> 重新上传出来的副本，**`noforwards` 标记消失了**。
> 也就是说：从受限频道下载再上传到你的频道后，这条新消息**可以被再次转发出去**。
> 如果源频道开了「禁止转发」，直接转发会被拒，但"下载+重传"的副本可以。

如果你的目的只是「把内容搬到自己的频道存档」，这一点没有价值；如果你的目的是
「搬进自己的频道，之后还要往外分发」，这才是有意义的差别。

### 0.2 和你的策略②比，它其实是降级方案

| 维度 | 你的策略②（UserBot 服务端直接转发） | 本项目（下载 + 重传） |
| --- | --- | --- |
| 带宽占用 | **0**（服务端搬运，字节不过你的机器） | 下载一份 + 上传一份 |
| 磁盘占用 | **0** | 峰值 ≈ 队列深度 × 单文件（它设 `Queue(maxsize=20)`） |
| 单条耗时 | 一次 RPC，毫秒级 | 由文件体积和带宽决定 |
| 文件大小上限 | 不受上传限制 | 4GB（Premium）/ 2GB（普通） |
| 失败面 | 主要是权限 | 权限 + 磁盘 + 网络 + FFmpeg + 引用过期 |
| 发帖人 | `drop_author` 后显示为频道/账号本身 | 上传账号 |
| `noforwards` 副本 | 保留源标记 | **标记消失，可再转发** |

**所以：不要因为它 README 说得玄乎就改用它的路径。** 你的 ①② 两级已经比它快一个数量级，
③ 只是兜底。真正该拿的是下面三个工程细节——我核实了你的代码，这三处**确实缺**。

### 0.3 你的代码里确实缺的三处（已核实）

**① 传输卡死没有检测**

`grep -n "timeout|wait_for|stall|stuck" core/fast_telethon.py` → **零命中**。

它的做法（`main.py:187`）：

```python
async def run_with_watchdog(task_coro, task_type, timeout_sec=60):
    task = asyncio.create_task(task_coro)
    last_bytes, stuck_count = -1, 0
    max_stuck = max(1, timeout_sec // 5)
    while not task.done():
        done, _ = await asyncio.wait([task], timeout=5)
        if done: break
        current = watchdog_data[f'{task_type}_bytes']     # 进度回调里更新
        stuck_count = stuck_count + 1 if current == last_bytes else 0
        last_bytes = current
        if stuck_count >= max_stuck:                       # 连续 N 个 5s 无字节增长
            task.cancel()
            raise TimeoutError("Stall detected")
    return task.result()
```

关键点不是「有超时」，而是**用"字节数是否增长"判断卡死**，而不是用总时长——
大文件本来就要跑很久，用总时长会误杀；用「连续 60s 没有任何字节进展」才准。

**收益**：死连接不再让整个克隆永久停摆。你现在遇到的情况是任务显示 `running`、
进度条不动、日志无报错、只能重启容器。
**成本**：~40 行，改 `core/fast_telethon.py` 的下载/上传 worker 调用处。
**风险**：阈值给太激进会误杀慢速网络——建议默认 90s 无进展才判定（它用 60s）。

**② `file_reference` 过期没被识别**

`grep -rn "file_reference|FileReference"` → **全仓库零命中**。

Telegram 的媒体引用有时效（约 1 小时，且换 session 会失效）。你的策略③这样处理：

```python
except Exception as e:
    logger.warning("策略3: msg=%s 异常: %s", msg_id, e)
    return None          # ← 引用过期被吞在这里
```

返回 `None` → 落到下一层 → 全部失败 → 发 `#fail2forward` → **这条消息永久丢失**。
而实际上重取一次消息就能拿到新引用，继续下。

它的做法（`download_message():451`）：

```python
FATAL_ERRORS   = ["self-destructing", "media_empty", "file_id_invalid", "message_id_invalid"]
EXPIRED_ERRORS = ["file reference", "file_reference_expired"]

if any(t in err.lower() for t in FATAL_ERRORS):
    return ... "SKIP"            # 直接跳过，不浪费重试
if any(t in err.lower() for t in EXPIRED_ERRORS):
    if retry_count > 5: return ... "SKIP"
    await asyncio.sleep(2)
    continue                      # ← 重新 get_messages 拿新引用再下
```

**收益**：长跑任务里「莫名其妙失败几十条大文件」的现象基本消失。
**成本**：~25 行（在策略③捕获 `errors.FileReferenceExpiredError`，重取消息后重试 1-2 次）。
**风险**：极低。注意别用字符串匹配（它是 `str(e).lower()` 判断，很脆），
你直接用 `except errors.FileReferenceExpiredError` 这个异常类即可——你已经在用异常类映射了。

**③ 下载与上传严格串行**

你的策略③是「取消息 → 下载 → 上传 → 下一条」，一个文件的下载和上传之间没有重叠。
它用两个 worker + 两个队列做流水线：

```python
fetch_queue  = asyncio.Queue(maxsize=3)    # 待下载批次
upload_queue = asyncio.Queue(maxsize=20)   # 待上传批次
dl_task = asyncio.create_task(downloader_worker(client_dl, fetch_queue, upload_queue))
up_task = asyncio.create_task(uploader_worker(client_dl, client_up, upload_queue))
```

效果：上传第 N 个文件时，第 N+1 个已经在下载。对 1GB 级别的大文件，理论吞吐接近翻倍
（下载和上传是两个方向的带宽，互不抢占）。

**收益**：只对走策略③的任务有效（`noforwards` 源、UserBot 不在目标频道、超 4GB）。
但恰恰这些场景下 ③ 是唯一路径，所以值得做。
**成本**：~80 行，要在 `Syncer` 里把「逐条 forward」重构成「生产/消费两段」。
**风险**：中等——增加了内存队列与磁盘占用，任务取消/暂停的语义要重新理一遍
（队列里积压的批次必须能干净丢弃）。**建议等 ①②做完、确认稳定性后再做。**

### 0.4 它不值得抄的部分

| 不抄 | 原因 |
| --- | --- |
| `tracker.py`（每题一条消息都 `json.load` → 改 → `json.dump` 全量重写，无锁） | 已有 SQLite + WAL，这是明确倒退 |
| Web Dashboard + SSH 隧道公网地址 | 你有 Telegram 菜单，凭空多一个攻击面；`lhr.life` 这类隧道也不适合常驻 |
| 双账号 | 只有一个 UserBot。真要降 FloodWait，**任务队列（R1）比加账号更安全** |
| GIF→MP4 / 给所有视频生成缩略图 / webp→jpg | 原生转发已保留原属性；只在策略③的降级路径里才有意义，收益远小于风险 |
| 按文件大小切换账号（>1950MB 用另一个号） | 单账号场景无意义 |
| 全局 `flood_ban_count` 只增不减 | 跑久了退避会涨到 1+20×0.5=11 倍，且永不恢复。**你现在的 `_interval_scale` 有同样的毛病**（见 R7），两个都该改成可衰减 |
| README 与代码不一致（`[PERFORMANCE]` 段代码根本不读，`FAST_THRESHOLD_MB` 两个值） | 反面教材：配置项必须由代码定义，不能靠文档 |

---

## 第一部分：七个来源各拿一条

| 来源 | 类型 | 最值得拿的那一条 | 见编号 |
| --- | --- | --- | --- |
| python-telegram-bot | 框架 | 四段式生命周期（`post_shutdown` 里收尾）+ 全局 `add_error_handler` | R2 / R3 |
| pyTelegramBotAPI | 框架 | `RETRY_ON_ERROR` 的分级重试思路（默认关，按错误类型决定重试） | R6 |
| node-telegram-bot-api | 框架 | 409 `Conflict` **自动删 webhook 自愈** | R8 |
| mirror-leech | 同类项目 | `asyncio.Event` 挂起式任务队列 + 三段并发额度 | R1 |
| tg-message-clone | 同类项目 | `frozen_config` 任务级配置快照 + 有序 `should_skip` 规则链 | F1 / F2 |
| KPSML-X | 同类项目 | 声明式 `arg_parser` + `themes` 文案层 | M2 / M3 |
| TelegramChannelCloner | 同类项目 | **字节增长式卡死看门狗** | R4 |

---

## 第二部分：完整候选清单

推荐度：**★★★ 强烈建议**（收益大且成本低）/ **★★ 值得做** / **★ 可选** / **✗ 不建议**

### A. 稳定性与抗封（收益最高的一类）

| 编号 | 来源 | 做什么 | 收益 | 成本 | 风险 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- |
| **R1** | mirror-leech | 任务队列：`asyncio.Event` 挂起 + `QUEUE_ALL` / `per_source` 额度 + `force` 插队；`tasks.status` 增加 `queued` | 同时起 N 个克隆时，实际请求频率 = 设计值 × N，FloodWait 乘积式到来。排队后频率恒定 | ~120 行 + 3 处接线 + 1 个迁移 | 低。默认并发 1，行为变化对用户是"变慢但更稳" | ★★★ |
| **R2** | python-telegram-bot | `SIGTERM` 处理：捕获信号 → 停止新任务 → 落盘 → `db.close()`（含 WAL checkpoint） | 目前 `grep signal` 零命中，`docker stop` 时 `finally` **根本不执行**，最近 5 秒统计丢失 + WAL 未 checkpoint，`stop_grace_period` 白配 | ~40 行，只动 `main.py` 收尾 | 低 | ★★★ |
| **R3** | python-telegram-bot | 全局错误兜底装饰器 `@guard()`：捕获未处理异常 → 日志 + 给用户回一句"出错了" | 现在 26 个 handler / 27 处局部 `except`，漏网的异常在用户侧**完全静默**（上次"点了没反应"就是这个温床） | ~30 行 + 逐个加装饰器 | 低 | ★★★ |
| **R4** | TelegramChannelCloner | 传输看门狗：连续 N 秒**字节数无增长** → cancel + 重试 | `fast_telethon.py` 现在零超时，死连接永久挂起，任务显示 `running` 但不动 | ~40 行 | 中低。阈值太激进会误杀慢速网络，建议 90s | ★★★ |
| **R5** | TelegramChannelCloner | 捕获 `FileReferenceExpiredError` → 重取消息 → 重试（1-2 次） | 现在全仓库零处理，被 `except Exception` 吞掉 → 消息永久丢失 | ~25 行 | 极低 | ★★★ |
| **R6** | TelegramChannelCloner + telebot | 错误分级：致命（跳过，不重试）/ 引用过期（立即重取重试）/ 瞬时（退避重试） | 现在所有异常一律 `return None` 落到下一层，重试预算不分级 | ~30 行 | 低 | ★★ |
| **R7** | PTB `AIORateLimiter` + 自查 | 限流器三项：① 配置缓存（现在每次 `wait()` 都 `yaml.safe_load`）② 分桶（全局 + per-chat）③ FloodWait 后倍率**可衰减** | ②防止单频道刷爆额度；③你现在的 `_interval_scale` 翻倍后**永不恢复**，跑久了恒定慢 2 倍甚至更多 | ~60 行 | 低 | ★★★ |
| **R8** | node-telegram-bot-api | 单实例锁 + `ConflictError` 专判：遇到就明确报"另一个进程在用同一 token"，而不是无限重连 | 现在零处理，容器双开时表现为莫名其妙的掉线循环 | ~30 行 | 低 | ★★ |
| **R9** | KPSML-X `alive.py` | heartbeat 文件 + `HEALTHCHECK`：由**真正处理消息的循环**打点 | 现在 Dockerfile / compose **无 HEALTHCHECK**，进程假死（Telethon 卡死但进程在）watchtower 永远不会重启 | ~30 行 + 2 行 yaml | 低 | ★★★ |
| **R10** | TelegramChannelCloner | 下载/上传流水线重叠（`Queue` + 双 worker） | 策略③串行，大文件吞吐腰斩；noforwards 源只能走③时收益最大 | ~80 行，需重构 syncer 循环 | 中。取消/暂停语义要重理 | ★★ |

### B. 可维护性

| 编号 | 来源 | 做什么 | 收益 | 成本 | 风险 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- |
| **M1** | KPSML-X + 自查 | 命令目录**单一来源**：`COMMANDS` 定义一次，`/help`、菜单说明、`main.py::BotCommand` 三处都从它生成 | 现在命令表写三遍，已经出现过不同步 | ~60 行 | 低 | ★★★ |
| **M2** | KPSML-X `themes` | 文案层 `bot/texts.py` | 现在 `handlers.py` 329 处 + `menu.py` 177 处含中文字面量 = **506 处**，改一句文案要 grep 全仓 | 分步：先收 `/help` 与错误提示（~80 行），再逐步 | 低 | ★★★ |
| **M3** | KPSML-X `arg_parser` | 参数解析 spec 化（带类型），保留现有位置式 `100-1100` 语法做兼容层 | 现在两套手写正则 + 十几个边界测试用例，加一个 flag 要改解析器 + 加测试 | ~90 行 | 中。改解析器容易把现有语法改坏，需保留回归用例 | ★★ |
| **M4** | mirror-leech | 状态适配器 + 注册表 + 分页 | 三个 `render_*` 各写一套进度逻辑；`limit=12` 是硬截断不是分页 | ~120 行 | 中，动渲染层 | ★★ |
| **M5** | mirror-leech `CustomFilters` | 权限装饰器 `@admin_only` / `@guard(admin=True)`，为分层角色（owner/admin/viewer）留位 | 现在 25 处重复 `if not await _require_admin(event): return` | ~25 行 | 极低。可与 R3 合并一次改完 | ★★★ |
| **M6** | PTB `ConversationHandler` + persistence | 会话状态统一 `ConversationStore` + `/cancel` + 落库（重启不丢） | 现在三套内存字典（`_wizards` / `_chapd_wait` / `_chaid_wait`），无 `/cancel`，过期靠下条消息被动清理，重启全丢 | ~150 行 | 中，动交互层 | ★★ |
| **M7** | mirror-leech `Config` | Config schema：类型表 + 默认值 + 单次加载 + 运行时 `set()` 可持久化 | 裸 dict，键名写错只在运行时 `KeyError`；`/settings` 目前只读 | ~100 行 | 低，但要逐个迁移读取点 | ★★★ |

### C. 功能与可观测性

| 编号 | 来源 | 做什么 | 收益 | 成本 | 风险 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- |
| **F1** | tg-message-clone | `should_skip(msg) -> (bool, reason)`，`skip_reasons` 计数落库 | 现在只有 `skip_count` 一个总数，说不出"跳过 12 条"是哪 12 条为什么 | ~50 行 | 低 | ★★ |
| **F2** | tg-message-clone `frozen_config` | `CloneOptions` frozen dataclass 落库（`options_json` 列） | 续跑时配置一变，同一任务前后行为不一致 | ~60 行 + 1 个迁移 | 低 | ★★ |
| **F3** | tg-message-clone | `-min-size / -max-size / -link / -nolink` 过滤 | 现在只能按 ID / 时间范围，无法"只克隆小于 2G 的" | ~50 行（依赖 M3） | 低 | ★★ |
| **F4** | mirror-leech `add_incomplete_task` | 重启后主动发消息告知"已从游标 X 续传" | 现在 `_auto_resume_clones` 成功只 `logger.info`，用户以为任务丢了 | ~20 行 | 极低 | ★★★ |
| **F5** | 自查 | `message_map` 加 `UNIQUE(task_id, source_msg_id)` | 现在只有普通索引；`/kljx <链接> <ID>` 允许重发，重试写两次就脏 | ~15 行 + 1 个迁移 | 低（老库可能已有重复行，迁移要先去重） | ★★ |
| **F6** | TelegramChannelCloner | 每个任务一份结构化统计报告（耗时/峰值速率/平均文件大小/失败清单） | 现在完成消息有几行，但没有可回看的报告 | ~50 行 | 低 | ★★ |
| **F7** | TelegramChannelCloner `chat_finder` | `/chats` 列出 UserBot 全部会话 + 数字 ID | 现在 `/chapd` 要转发一条消息才行，找频道得先有消息 | ~40 行 | 低 | ★ |
| **F8** | TelegramChannelCloner | 双账号（下载/上传分离） | 能把 FloodWait 风险摊到两个号 | ~120 行 | **高**。需要第二个真实账号，且两个号都可能被限 | ✗ 有 R1 就不需要 |
| **F9** | TelegramChannelCloner | GIF→MP4 / 全量缩略图 / webp→jpg | 只在策略③降级路径有意义 | ~80 行 + FFmpeg 依赖 | 中（CPU + 失败面） | ✗ |

### D. 明确不做

| 不做 | 原因 |
| --- | --- |
| webhook 模式（PTB 推荐） | 你的瓶颈是搬运不是收消息延迟；webhook 还要公网 + 证书 |
| 换框架（PTB / telebot / node-telegram-bot-api） | UserBot、Takeout 导出、服务端转发这三件核心能力，Bot API 框架都做不到 |
| telebot 的线程池模型 | 与现有 asyncio + aiosqlite 冲突，混用会引入锁问题 |
| mirror-leech 的 `helper/common.py`（53KB 上帝类） | 与你「拆成 forwarder / syncer」的现状相比是倒退 |
| mirror-leech 的全局可变状态（`from .. import download_dict`） | 你用构造注入已经把依赖显式化，别退回隐式全局 |
| KPSML-X 的容器内 `git reset --hard` 自更新 | 与 CI→镜像→watchtower 的不可变部署冲突，回滚失效 |
| MongoDB | 单机场景 SQLite + WAL + 备份已经更合适 |
| uvloop | 5-15% 的 asyncio 开销收益，远小于上面任何一项；收益/风险比最差 |
| TelegramChannelCloner 的 Web Dashboard / SSH 隧道 | 有 Telegram 菜单，多一个公网入口是净负担 |
| 用「下载+重传」替代策略② | 见第 0.2 节：带宽、磁盘、耗时全面更差 |

---

## 第三部分：三个套餐（省事的选择方式）

如果不想逐条挑，可以按套餐走：

### 套餐 A · 稳健优先（推荐先做这个）

```
R2 优雅停机  +  R3 全局错误兜底  +  R9 HEALTHCHECK  +  F4 重启通知  +  M5 权限装饰器
```

- 全部是「加东西、不改现有逻辑」的低风险改动，合计约 **150 行**。
- 效果：容器重启不再丢数据、异常不再静默、假死能被发现、用户知道任务还在。
- **不含任何行为变化**，跑完现有 172 个测试应该全绿，不需要新写测试（但要补 3-4 个针对性用例）。

### 套餐 B · 抗封优先（跑大任务前做）

```
R1 任务队列  +  R7 限流器三项  +  R4 看门狗  +  R5 引用过期重试
```

- 合计约 **245 行**，其中 R1 会改变多任务的行为（变慢但稳定）。
- 效果：多任务不再互相叠加速度、死连接自己恢复、长跑不再零星丢消息。
- 建议在套餐 A 之后做，因为 R1 需要 R7 的 per-chat 分桶才完整。

### 套餐 C · 可维护性（不急，但每加一个功能就更疼）

```
M1 命令目录单一来源  +  M2 文案层  +  M7 Config schema  +  M5 权限装饰器
```

- 合计约 **265 行**。
- 效果：以后加命令/改文案/改配置的成本从「改三个地方」变成「改一个地方」。
- M2 可以分步做，不必一次收完 506 处。

### 其余（M3 / M4 / M6 / R10 / F1-F7）

都是「锦上添花」，建议等你确认真实痛点后再单独做：
- 觉得参数记不住 → M3
- 任务多到一页显示不完 → M4
- 向导经常过期 → M6
- 策略③跑得慢 → R10
- 老问"为什么少了消息" → F1

---

## 第四部分：我的建议

**如果只做一件事：R1（任务队列）。**
它对应的不是「优化」而是「止损」——`RateLimiter` 是每个 `Syncer` 实例一个，
你不限制并发，用户点两次 `/kl` 就等于把限流参数砍半，点五次就等于不设防。
这解释了为什么"多任务一起跑的时候封控特别快"。

**如果做三件事：R1 + R2 + R9。**
一个管频率，一个管收尾，一个管假死。三件加起来约 190 行，覆盖了目前所有「需要重启容器才能恢复」
的场景。

**R4 / R5 是低成本高回报的补丁**（合计 65 行），建议顺手带上——
它们修的正是"任务显示 running 但不动"和"零星丢几十条大文件"这两个你迟早会遇到的症状。
