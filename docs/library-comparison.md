# 三个 Telegram Bot 框架对比，以及可借鉴到本项目的设计

对比对象：`node-telegram-bot-api`（Node.js）、`pyTelegramBotAPI`（telebot）、`python-telegram-bot`（PTB v21+）。
目的不是选型（本项目用 Telethon，直连 MTProto，能力集不同），而是**从成熟框架的架构里挑出能治本项目痛点**的做法。

---

## 0. 一页结论

| 框架 | 定位 | 一句话架构 | 最值得抄的一点 |
| --- | --- | --- | --- |
| node-telegram-bot-api | 薄封装 + EventEmitter | `Bot` 收更新 → `emit('message')` → 你挂的回调 | **409 冲突自动解**：polling 检测到 webhook 占用会 `deleteWebHook` 后继续 |
| pyTelegramBotAPI | 同步/异步双栈，装饰器注册 | `@bot.message_handler(filters)` 按注册顺序匹配 | **`register_next_step_handler` + 中间件**：多步流程与跨切面逻辑都是一等公民 |
| python-telegram-bot | 全功能框架 | `Application`(Builder) 持 `Updater` + `JobQueue` + handler 分组 | **分层治理**：全局 error handler、handler 分组优先级、`AIORateLimiter` 双层令牌桶、`ConversationHandler` 显式状态机 |

一句话：**node 版赢在“冲突自愈”，telebot 赢在“上手成本”，PTB 赢在“工程化”**。本项目最该抄的是 PTB。

---

## 1. 更新接收：polling / webhook

| | node-telegram-bot-api | pyTelegramBotAPI | PTB |
| --- | --- | --- | --- |
| 默认 | `polling: true`，间隔 300ms，long polling `timeout: 10` | `bot.polling(none_stop=False, interval=0, timeout=20)` | `Application.run_polling()`，内部 `Updater` |
| webhook | 内置 HTTPS server，可给 `healthEndpoint: '/healthz'` | `set_webhook(url, certificate=...)`，需自己起服务器 | 内置 Tornado server，或自定义（把 update 塞进 `update_queue`）|
| 409 冲突 | **自动 `deleteWebHook` 后重试** | `none_stop=True` 时不因错误停轮询 | 抛出 `Conflict`，让上层决策（通常意味着起了两个实例）|
| 偏移管理 | 自己维护 `offset = update_id + 1` | 同上 | 同上，另可 `drop_pending_updates` |

**借鉴点**：409 不是“网络抖动”，是**部署事故**（两个副本 / 残留 webhook）。三家的处理方式不同，但共识是“必须显式识别”，node 版直接自愈，PTB 直接抛错——都比静默重试好。

**本项目现状**：`main.py` 有指数退避重连（`RECONNECT_BASE_DELAY * 2^(n-1)`，上限 `RECONNECT_MAX_DELAY`），但**没有识别 409 / 多实例**。Telethon 侧对应 `errors.ConflictError`（另一个实例在用同一 bot token）和 `errors.AuthKeyDuplicatedError`。现在这两种错误会被当成普通异常反复重连，日志刷屏但不告诉你根因。

---

## 2. Handler 路由与优先级

**node-telegram-bot-api**：纯 EventEmitter。`bot.on('message')`、`bot.on('callback_query')`，多监听器**全部**触发（`onlyFirstMatch: true` 才停）。优点是零抽象，缺点是“谁先谁后”靠你 `on` 的顺序，且没有过滤器概念，判断逻辑全写回调里。

**pyTelegramBotAPI**：装饰器 + 过滤器链（`commands` / `content_types` / `regexp` / `func`），**按注册顺序**匹配，命中即停（默认）。过滤器可组合，自定义过滤器通过 `bot.add_custom_filter(...)` 注册，权限校验可以做成 `IsAdminFilter` 而不是每个 handler 里写 `if not is_admin: return`。

**PTB**：`Handler` 对象 + **group 分组**。`Application.add_handler(handler, group=0)`，group 小的先跑；同一 group 内**只执行第一个匹配的**，不同 group 会继续传递。这套语义解决了“全局拦截器（鉴权/限流/日志）不应该和业务 handler 抢消息”的问题：拦截器放 group=-1，业务放 group=0。

**借鉴点**：① 权限是**过滤器/前置层**，不是每个 handler 的第一行；② 分组让“旁路逻辑”与“主流程”解耦，而不是靠注册顺序碰运气。

**本项目现状**：26 个 `@bot.on(...)`，**25 处**重复写 `if not await _require_admin(event): return`；会话拦截器（`/chapd`、`/chaid` 的转发监听）与业务 handler 混在同一 group 里，全靠“谁先注册”。`bot/handlers.py` 已 2232 行，新增命令时漏写 `_require_admin` 的风险是真实存在的。

---

## 3. 多步会话 / 状态机

| | 机制 | 持久化 | 取消 |
| --- | --- | --- | --- |
| node-telegram-bot-api | 无。自己拿 `chatId → state` 存 Map | 无 | 自己实现 |
| pyTelegramBotAPI | `register_next_step_handler(msg, fn)`，可带参数；`enable_save_next_step_handlers()` 落盘 | 可选（文件/Redis）| 需自己写 |
| PTB | `ConversationHandler`：`entry_points` / `states` / `fallbacks`，每用户独立状态 | `BasePersistence`（Pickle/Dict/自定义）| `fallbacks` 里放 `/cancel` 即标准做法 |

**借鉴点**：状态机要**显式声明状态集合与转移**，并且必须有 **fallback（取消/超时）**。隐式字典的问题是：没人知道合法状态有哪些、超时靠每次消息进来时比较 timestamp、进程重启就丢。

**本项目现状**：三套并存 —— `menu.MenuController._wizards`（`Wizard(kind, step, data, ts)`，15 分钟）、`handlers._chapd_wait`、`handlers._chaid_wait`（各 10 分钟，`time.time()` 比较）。共同问题：① 过期条目**只在下次消息进来时被动清理**，没人来就永久驻留；② 没有 `/cancel`；③ 同一用户可同时处于多个流程（`/chaid` 后再 `/chapd`，两条监听都挂着）；④ 重启即丢，用户正输到一半的向导直接失效且无提示。

---

## 4. 错误处理

| | 机制 |
| --- | --- |
| node-telegram-bot-api | 无全局处理器；polling 错误走 `polling_error` 事件，API 调用错误是 Promise rejection——**漏 catch 就是 unhandledRejection**（`badRejection` 选项专门为此存在）|
| pyTelegramBotAPI | `@bot.exception_handler`（全局收异常），`apihelper.RETRY_ON_ERROR`（默认 **False**）、`MAX_RETRIES`（同步 15 / 异步 3），`util.antiflood(fn, *args, number_retries=5)` 专门吞 429 并按 `retry_after` 重试 |
| PTB | `Application.add_error_handler(cb)`：handler/job 里抛的异常统一进这里，带 `CallbackContext.error` 与 traceback；网络层另有 `networkloop` 对 `RetryAfter` / 网络错误做有界重试 |

**借鉴点**：**错误处理必须是框架级的兜底，而不是每个 handler 的自觉**。业界的默认做法是三层——① 网络层重试（可恢复错误）；② handler 层兜底（记 traceback + 给用户一句“出错了，编号 xxx”）；③ 告警层（同一错误 N 分钟内只报一次，避免刷屏）。

**本项目现状**：`bot/handlers.py` 里 27 个 `except Exception`，全部是**局部**处理——也就是说，任何一个没被包住的 await（比如 `event.reply` 本身超时、`models.get_task` 抛错）都会冒到 Telethon 的 loop 异常处理器，用户侧**没有任何反馈**：点了按钮，转圈，然后什么都没有。这正是上一轮“实时监控建了却没提示”那类问题的温床。

---

## 5. 网络层与重连

- **node 版**：完全依赖 `request` 库，无重试策略，无退避。production 里通常要自己包一层。
- **telebot**：`CONNECT_TIMEOUT=15`、`READ_TIMEOUT=30`，`RETRY_ON_ERROR` 默认关；session 按线程 local 复用（`SESSION_TIME_TO_LIVE=600` 轮换）。重连本质是 `polling()` 外层 while。
- **PTB**：`HTTPXRequest(connect_timeout, read_timeout, write_timeout, connection_pool_size)`；`getUpdates` 用**独立**的连接池（默认 1），避免长轮询占满业务请求的池子——这是个容易忽略但很实用的细节：**长轮询连接与业务请求连接必须隔离**，否则一条 50s 的长轮询会把 pool 里其他请求挤住。

**借鉴点**：① 超时要分连接/读/写三种，不能一个 `timeout=30` 打天下；② 长轮询通道与业务通道隔离；③ 退避要有上限（本项目的 `RECONNECT_MAX_DELAY` 已经对了）。

---

## 6. 限流：被动挨打 vs 主动预算

| | 做法 |
| --- | --- |
| node 版 | **无**。全靠 Telegram 返回 429 再自己处理 |
| telebot | `antiflood()` 工具函数：收到 `TooManyRequests` 按 `retry_after` 睡再重试（仍是**被动**）|
| PTB | `AIORateLimiter`：`aiolimiter` 令牌桶，**双层** —— 全局 `overall_max_rate=30 / 1s`，按群 `group_max_rate=20 / 60s`；`group_id` 由参数里有没有 `chat_id` 自动推断；收到 `RetryAfter` 会**暂停所有请求** `retry_after + 0.1s` |

**借鉴点**：限流要**按目标分桶**（每个群/频道一个桶 + 一个全局桶），因为 Telegram 的配额本来就是按 chat 算的。单纯“每条 sleep 2~5 秒”会同时犯两个错：公共频道被无谓拖慢，而某个群刷得快时又挡不住。

**本项目现状**：`core/rate_limiter.py` 是**单一全局计数器** + 固定随机间隔（`forward_interval`）+ 每 N 条长歇。三个具体问题：

1. **每次 `wait()` 都读一次 `config.yaml`**（`_current_limits()` → `yaml.safe_load(open(...))`）。克隆 10 万条 = 10 万次磁盘 I/O + YAML 解析，纯浪费。
2. **没有 per-chat 分桶**：同时跑“频道 A → 群 1”和“频道 B → 群 2”时，两个任务共享一个 `_count`，节奏互相拖累；而单个群刷太快时也没有独立预算。
3. **`_interval_scale` 在 FloodWait 后翻倍但永不恢复**：一次 429 会把整个进程的节奏永久放慢一倍（重启才复位）。

---

## 7. 并发模型

- node：单线程事件循环，回调里做重活就阻塞一切。
- telebot：`TeleBot` 用 `ThreadPoolExecutor` 跑 handler；`AsyncTeleBot` 走 asyncio。同步版里共享状态要自己加锁。
- PTB：纯 asyncio；默认**串行**处理 update，可换 `BaseUpdateProcessor` 并发；`JobQueue` 内部用 APScheduler 管理调度。

**借鉴点**：默认串行 + 可选的并发处理器，比“默认并发、出事再限流”安全得多。对本项目意义在于：**Telethon 的 handler 是并发调度的**，两个用户同时点“确认”时，共享的 `sync_tasks` 字典与 DB 连接都可能竞争——这是 PTB 显式串行想避开的问题。

---

## 8. 生命周期与可观测性

- node：`startPolling` / `stopPolling`（`cancel` 可中断在途请求）；webhook 有 `/healthz`。
- telebot：`polling()` 阻塞直至 `stop_polling()`；`none_stop` 控制遇错是否继续。
- PTB：`Application.initialize() → start() → stop() → shutdown()` 四段式，`post_init` / `post_stop` 钩子；`JobQueue` 的 job 由框架统一取消。

**借鉴点**：**停机是框架职责**。四段式生命周期保证：停止收新 update → 等在途任务 → flush 统计 → 关连接。任何一步缺失都会丢数据。

**本项目现状（真实缺陷）**：`main.py` 只在 `finally` 里 `await db.close()`（内部 `wal_checkpoint(TRUNCATE)`），但**没有安装 SIGTERM 处理器**（`grep signal` 为 0 命中）。`docker compose down` / `docker restart` / watchtower 重建容器时，Docker 发的是 **SIGTERM**，Python 默认行为是直接终止进程，**`finally` 不会执行**。后果：

- 监控的 `bump_stats` 内存计数（每 5 秒才 flush 一次）丢失最近 5 秒；
- 正在跑的克隆任务 `status` 停在 `running`，重启后靠 `auto_resume` 兜底（有，但依赖配置）；
- WAL 未合并（虽然下次打开会回放，但宿主机上那份 `bot.db` 在容器停止的瞬间不是一致的）。

`docker-compose.yaml` 里配了 `stop_grace_period`，但**没装信号处理器，宽限期等于白给**。

---

## 9. 可以落到本项目的改进（按优先级）

> 只列“收益明确、改动可控、可测”的项。每条给出：现状 → 改法 → 验收。

### P0-1 全局错误兜底 + 用户可见的失败反馈

现状：27 个局部 `except`，漏网的异常对用户完全静默。

改法：`bot/safe.py`

```python
import logging, time, uuid
from functools import wraps

logger = logging.getLogger("tg_forward_bot.handler")
_last_notify: dict[tuple[int, str], float] = {}

def safe_handler(fn):
    @wraps(fn)
    async def wrapper(event, *a, **kw):
        try:
            return await fn(event, *a, **kw)
        except Exception as e:
            ref = uuid.uuid4().hex[:8]
            logger.exception("handler %s 失败 ref=%s", fn.__name__, ref)
            key = (getattr(event, "chat_id", 0), type(e).__name__)
            if time.time() - _last_notify.get(key, 0) > 60:   # 同一错误 60s 内只提示一次
                _last_notify[key] = time.time()
                try:
                    await event.reply(f"❌ 处理失败（编号 {ref}），已记录日志")
                except Exception:
                    pass
            return None
    return wrapper
```

应用方式：不必改 26 处函数体，只在注册处统一包一层——把 `@bot.on(...)` 换成 `@bot.on(...)` + `@safe_handler`（Telethon 允许叠加装饰器，`@bot.on` 返回原函数）。**验收**：`test/test_safe_handler.py` 注入一个必抛异常的 handler，断言回复含编号、且同错误第二次不再回复。

### P0-2 SIGTERM 优雅停机（修真实缺陷）

现状：无信号处理，`finally` 在容器停止时不执行。

改法：`main.py`

```python
import signal

stop_event = asyncio.Event()
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, stop_event.set)

# 主循环改为：await stop_event.wait() 与 client.run_until_disconnected() 竞速
# 停机顺序：stop_event → monitor_manager.stop_all() → flush 所有统计 → db.checkpoint() → disconnect
```

**验收**：容器内 `docker stop`（默认 10s 宽限），日志出现 `已 flush 统计 / WAL checkpoint 完成`；再起容器，`done_count` 与停机前一致。

### P0-3 限流器：配置缓存 + per-chat 令牌桶

现状：每条消息读一次 YAML；全局单桶；FloodWait 后永久变慢。

改法：`core/rate_limiter.py`

```python
# 1) 配置缓存：按 mtime 失效，而不是每次 open
_mtime, _cache = 0.0, {}
def _get_dynamic_rate_limit(cfg):
    global _mtime, _cache
    try:
        m = os.stat("config.yaml").st_mtime
        if m != _mtime:
            _cache = yaml.safe_load(open("config.yaml", encoding="utf-8")).get("rate_limit", {})
            _mtime = m
        return _cache
    except Exception:
        return cfg.get("rate_limit", {})

# 2) 分桶：全局 30/s + 每群 20/min（对齐 PTB AIORateLimiter 的语义）
self._global = AsyncLimiter(30, 1)
self._per_chat: dict[int, AsyncLimiter] = {}
```

另外把 `_interval_scale` 改成**指数衰减**（每次成功后 ×0.98，低于 1.0 截断），而不是永不复位。
**验收**：`test/test_rate_limiter.py` —— 1000 次 `wait()` 只 stat 一次 config；两个不同 target 交替调用互不阻塞；FloodWait 后 50 次成功调用 `_interval_scale` 回到 1.0。

### P1-4 会话统一成 `ConversationStore`

现状：`_wizards` / `_chapd_wait` / `_chaid_wait` 三套。

改法：`core/conversation.py`

```python
@dataclass
class Conversation:
    kind: str            # "clone_wizard" | "chapd" | "chaid"
    step: str = ""
    data: dict = field(default_factory=dict)
    expires_at: float = 0.0

class ConversationStore:
    """一个用户同时只允许一个活跃流程；get() 时惰性清理过期项并返回 None。"""
    def begin(self, key, kind, *, ttl=600) -> Conversation: ...
    def get(self, key, *, kind=None) -> Conversation | None: ...   # 过期自动 pop + 可选通知
    def pop(self, key): ...
    def gc(self) -> int: ...     # 由 Job 每 60s 调一次，避免只靠“下条消息”清理
```

配套：`/cancel` 命令清除当前流程；`begin()` 时若已有流程，先回一句「已取消上一步：xx」。

**验收**：单测覆盖 TTL 过期、并发两种流程互斥、`/cancel`；线上验证：`/chaid` 后发 `/chapd`，第二条命令会提示已切换而不是两个监听同时等待。

### P1-5 handler 分组：拦截层与业务层分离

现状：26 个 handler 同组，靠注册顺序；25 处重复 `_require_admin`。

改法：Telethon 的 `@bot.on(event, group=N)` 支持分组（**低 group 先执行**，且不同 group 都会执行）。把“会话拦截器”（`/chapd`、`/chaid` 的转发捕获）放 `group=-1`，命令放 `group=0`，回调放 `group=10`。同时把鉴权改成装饰器：

```python
def admin_only(fn):
    @wraps(fn)
    async def w(event, *a, **kw):
        if not await _require_admin(event): return
        return await fn(event, *a, **kw)
    return w
```

这一步能把 `handlers.py` 里 25 行重复判断消掉，也让“谁先拿到消息”变成显式配置。

### P1-6 命令目录单一来源

现状：`/help` 文案、菜单「命令与说明」、`main.py` 的 `BotCommand` 列表**三处各写一遍**，已经出现过文案不同步（上一轮就同步改了三处）。

改法：`bot/commands.py`

```python
COMMANDS = [
    Command("kl", "克隆频道全部消息并持续监听", "/kl <链接>"),
    Command("kljx", "继续克隆（可带起始ID/区间）", "/kljx <链接> [ID|A-B]"),
    ...
]
def help_text() -> str: ...
def bot_commands() -> list[BotCommand]: ...
```

**验收**：改一处文案，`/help`、菜单、Telegram 命令面板同时更新；加一个单测断言 `len(COMMANDS) == len(bot_commands())`。

### P1-7 统一 Job 调度替代散落的 sleep 循环

现状：`core/monitor.py` 每个任务一个 `_stats_loop`（`while True: sleep(5); flush`），相册还有 `delayed_flush` 的 `create_task`。任务停止时靠手工 `cancel()`，漏一个就是“往已删除任务写统计”。

改法：`core/jobs.py` 一个 supervisor：

```python
class JobManager:
    def every(self, name, interval, fn, *, jitter=0.1, run_on_start=False): ...
    async def start(self): ...      # 单循环，按 next_run 排序，异常隔离（单个 job 抛错不影响其他）
    async def stop(self): ...       # 取消所有 job 并 await 收尾
```

收益：停机时一处 `await jobs.stop()` 全部收尾；`/settings` 可展示“当前 3 个 job、下次执行 2s 后”，排障从“猜”变成“看”。

### P2-8 `message_map` 加唯一索引（DB 级幂等）

现状：`idx_message_map_task` 是普通索引，允许同一 `(task_id, source_msg_id)` 重复插入。现在靠逻辑去重（游标 + `/kljx` 判断），但 `/kljx <链接> <ID>` 是**故意重发**的入口，一旦某次重试把同一条写了两次，映射就脏了。

改法：迁移里 `CREATE UNIQUE INDEX IF NOT EXISTS uq_message_map ON message_map(task_id, source_msg_id)`，写入用 `INSERT OR IGNORE`。重复克隆仍会发消息（用户要的），但映射表保持一条。

### P2-9 单实例锁 + Conflict 明确处理

现状：无锁；`ConflictError` 会被当成普通异常反复重连。

改法：启动时对 `data/bot.lock` 取 `fcntl.flock(LOCK_EX | LOCK_NB)`（Windows 用 `msvcrt.locking`），失败直接退出并打印「已有实例在运行」；`except errors.ConflictError` 单独分支——不重连，直接报错退出（说明 token 被别的进程占用）。

### 不建议照搬的三点

1. **webhook 模式**：本项目的瓶颈是“拉历史 + 大文件搬运”，不是收消息延迟；webhook 会引入公网入口、证书、反向代理，收益不匹配。
2. **把 Telethon 换成 Bot API 框架**：本项目依赖 UserBot（MTProto）读私有源、`forward_messages` 服务端搬运、Takeout，Bot API 根本做不到。可借鉴的是**架构**，不是运行时。
3. **telebot 的 ThreadPool 并发**：同步线程模型与现有 asyncio + aiosqlite 栈冲突，混用只会引入锁问题。

---

## 10. 建议的落地顺序

| 顺序 | 项 | 收益 | 风险 | 预估改动 |
| --- | --- | --- | --- | --- |
| 1 | P0-2 SIGTERM 停机 | 直接止住数据不一致 | 低（只动 main 收尾） | ~40 行 |
| 2 | P0-1 全局错误兜底 | 用户不再“点了没反应” | 低（装饰器，可逐组启用） | ~30 行 + 测试 |
| 3 | P0-3 限流器 | 克隆吞吐与稳定性同时提升 | 中（影响所有转发节奏，需压测） | ~60 行 |
| 4 | P1-6 命令目录 | 消除文案漂移 | 极低 | ~80 行 |
| 5 | P1-5 handler 分组 + 鉴权装饰器 | 降低新增命令的漏判风险 | 中（顺序敏感，需回归） | ~50 行 |
| 6 | P1-4 会话统一 | 修“多流程并存/重启丢状态” | 中 | ~150 行 |
| 7 | P1-7 Job 调度 | 收尾干净、可观测 | 中 | ~120 行 |
| 8 | P2-8 / P2-9 | 幂等与部署安全 | 低 | ~30 行 |

验收统一口径：① 全量单测通过（当前 172 例）；② `docker stop` 后重启，任务计数与状态一致；③ 手动跑一次 `/kl 100-200`，日志无 `Task was destroyed`、无未捕获异常。

---

## 11. 参考

- PTB 架构总览：<https://github.com/python-telegram-bot/python-telegram-bot/wiki/Architecture>
- PTB `AIORateLimiter`：<https://docs.python-telegram-bot.org/en/v22.0/telegram.ext.aioratelimiter.html>
- pyTelegramBotAPI handler/中间件：<https://github.com/eternnoir/pyTelegramBotAPI>
- pyTelegramBotAPI `antiflood`：<https://pytba.readthedocs.io/en/4.36.0/util.html>
- node-telegram-bot-api polling 与 409 处理：<https://github.com/yagop/node-telegram-bot-api/blob/master/doc/api.md>
- Telegram Bot API 限流说明：<https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this>
