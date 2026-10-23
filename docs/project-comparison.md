# 三个开源项目的架构对比与可借鉴点

> 分析对象：
> 1. [anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot)（4.3k★，Pyrogram，GPL-3.0，156 文件）
> 2. [yesanqian/tg-message-clone](https://github.com/yesanqian/tg-message-clone)（201★，Telethon，188KB）
> 3. [Tamilupdates/KPSML-X](https://github.com/Tamilupdates/KPSML-X)（102★/151 fork，Pyrogram，GPL-3.0）
>
> 对照项目：本项目（Telethon + aiosqlite，`bot/handlers.py` 2232 行 / `bot/menu.py` 981 行 /
> `core/forwarder.py` 1284 行 / `core/syncer.py` 576 行，172 个单测）
>
> **取证说明**：项目 1 与 3 的源码已完整读取（项目 3 的真实代码在其 `kpsmlx` 分支，`main`
> 只有 10 个引导文件）；**项目 2 的仓库只公开了 `guis/message_filters/` 一个子集**（9 个文件），
> 主程序与克隆核心未公开，因此对它的评价仅基于可见代码 + README/topics，不做推测性结论。

---

## 1. 三者速览

| 维度 | mirror-leech | tg-message-clone | KPSML-X |
| --- | --- | --- | --- |
| 定位 | 万能下载/上传/镜像（rclone、aria2、qBittorrent、yt-dlp、Google Drive） | 频道消息备份/克隆（带 GUI 筛选器） | 同 mirror-leech 的 Pyro 分支（wzml-x 系） |
| 框架 | Pyrogram | Telethon（+ Tkinter GUI） | Pyrogram（pyrofork 2.2.11） |
| 规模 | 156 文件 / 5.5MB，最长文件 `helper/common.py` 53KB | 可见 9 文件，核心未公开 | 104 文件（`kpsmlx` 分支），最长 `bot_settings.py` 56KB |
| 状态存储 | MongoDB（可关） | 未公开 | MongoDB / `config.env` |
| 进程模型 | 单进程 + 全局状态 + asyncio 队列 + APScheduler | 单进程 GUI | 单进程 + 容器内自动 git 更新 |
| 依赖重量 | 极重（aria2/qbit/nzb/jd/rclone 全内嵌） | 轻 | 重 |
| 授权 | GPL-3.0 | 无 license | GPL-3.0 |

一句话结论：**项目 1 是「任务调度」的教科书**，项目 3 是「命令与文案工程化」的样板，
项目 2 只有「筛选规则链 + 配置快照」两个点值得看，实现质量明显偏低。

---

## 2. 架构设计对比

### 2.1 mirror-leech：分层清晰，但用全局状态粘合

```
bot/
├── __init__.py          # 全局可变状态：download_dict / queued_dl / queued_up /
│                        #   task_dict / 10+ asyncio.Lock / APScheduler / uvloop
├── core/                # 生命周期：config_manager、startup、telegram_manager、torrent_manager
├── modules/             # 命令入口（每个命令一个 async 函数，只做参数解析 + 派发）
├── helper/
│   ├── listeners/       # 任务生命周期回调（onDownloadStart/onUploadComplete/onDownloadError）
│   ├── mirror_leech_utils/
│   │   ├── download_utils/   # 每种来源一个下载器，统一 async 接口
│   │   ├── upload_utils/     # 每种目标一个上传器
│   │   └── status_utils/     # 每种任务一个状态适配器（统一 gid/progress/speed/eta 接口）
│   ├── ext_utils/       # task_manager（队列）、db_handler、bot_utils、files_utils
│   └── telegram_helper/ # message_utils、button_build、filters、bot_commands
```

三个真正的架构优点：

1. **「下载器 / 上传器 / 状态类」三件套同构**。加一种来源 = 新增一个 `xxx_download.py`
   加一个 `xxx_status.py`，其余代码零改动。这是它能塞进 10+ 种传输后端还不崩的原因。
2. **命令入口极薄**。`modules/*.py` 里只做 `arg_parser` → 建 listener → 调 helper，
   业务逻辑全在 helper。对比本项目 `handlers.py` 2232 行里混着校验、解析、建任务、渲染。
3. **状态与渲染分离**。状态类只暴露 `name/size/progress/speed/eta/gid/status/eng`，
   渲染器只做「遍历 `download_dict` → 拼字符串 → 分页」，因此新增任务类型不用碰渲染代码。

### 2.2 KPSML-X：mirror-leech 的 Pyro 分支 + 三层工程化增强

目录结构与项目 1 高度相似（`helper/mirror_utils/*` 而非 `mirror_leech_utils`），真正的差异在：

| 增强 | 文件 | 作用 |
| --- | --- | --- |
| 主题层 | `helper/themes/__init__.py` + `kpsml_minimal.py` | `BotTheme('STOP_DUPLICATE', content=...)` 集中所有用户可见文案 |
| 声明式参数 | `ext_utils/bot_utils.py::arg_parser` | `arg_base` 即 schema，flag 与值自动分组 |
| 任务装饰器 | `bot_utils.py::new_task / new_thread / sync_to_async` | handler 立即返回，长任务丢给 event loop / 线程池 |
| 引导式部署 | `update.py` | 启动时 `git reset --hard origin/$UPSTREAM_BRANCH` 拉最新代码，配置来自 `CONFIG_FILE_URL` 或 Mongo |
| 存活探针 | `alive.py` | Web 端口保活（Heroku 场景） |

### 2.3 tg-message-clone：只有一个「过滤器层」是完整设计

可见代码就是一个纯函数式的过滤管线：

```
frozen_config（任务开始时冻结的用户配置）
    ↓
should_skip_message(message, frozen_config) -> bool
    ├─ MessageService                      → skip
    ├─ MessageMediaGiveaway / GiveawayResults → skip
    ├─ MessageMediaUnsupported 且无文案      → skip
    ├─ 文件大小区间（min_file_size ~ max_file_size） → skip
    ├─ 媒体类型（media_type_searcher.check）  → skip
    └─ 自定义规则（含链接 / 排除链接）        → skip
```

规则从**便宜到贵**排列，前三条是纯类型判断（O(1)），大小判断读 `message.file.size`，
链接判断才遍历 `entities`。这是正确的顺序设计，值得学。

### 2.4 架构对比总表

| 关注点 | mirror-leech | KPSML-X | tg-message-clone | 本项目现状 |
| --- | --- | --- | --- | --- |
| 分层 | core/modules/helper 三层 | 同上 + themes | filters 独立 | `bot/` + `core/` + `db/`，handler 偏厚 |
| 命令入口 | 极薄，走 `arg_parser` | 同 | 未公开 | 手写正则逐条解析 |
| 任务编排 | **asyncio.Event 队列 + 三段额度** | 同 | 无（单任务 GUI） | **无上限，直接 create_task** |
| 状态呈现 | 适配器 + 注册表 + 分页 | 同 | 无 | 3 个 `render_*` 各写一套 |
| 配置 | 类属性即 schema + 类型强转 + 热改 | dict + 按钮热改 | `frozen_config` 快照 | 裸 dict，每次 `yaml.safe_load` |
| 持久化 | Mongo（未完成任务可续） | Mongo | 无 | SQLite（已做 WAL + 备份，优于三者） |
| 文案 | 集中 `help_messages.py` 30KB | **themes 层** | 无 | **506 处中文字面量散在两个文件** |

---

## 3. 值得借鉴的具体点

### 3.1 来自 mirror-leech

#### ① 任务队列与全局并发额度（最高价值）

**它的做法**（`helper/ext_utils/task_manager.py`）：

```python
async def check_running_tasks(listener, state="dl"):
    all_limit = Config.QUEUE_ALL
    state_limit = Config.QUEUE_DOWNLOAD if state == "dl" else Config.QUEUE_UPLOAD
    event = None
    async with queue_dict_lock:
        is_over_limit = (all_limit and dl_count + up_count >= all_limit and ...) \
                        or (state_limit and t_count >= state_limit)
        if is_over_limit:
            event = Event()
            queued_dl[listener.mid] = event      # 挂起，不是拒绝
        if not is_over_limit:
            non_queued_dl.add(listener.mid)
    return is_over_limit, event
```

- 超额**不报错、不排队等待**，而是把任务挂在一个 `asyncio.Event` 上；任务自己
  `await event.wait()`，被放行时由 `start_dl_from_queued()` `set()` 唤醒。
- 三段额度：`QUEUE_ALL`（总并发）、`QUEUE_DOWNLOAD`、`QUEUE_UPLOAD`；
  `force_download / force_upload / force_run` 可插队（对应 `/fdl` 类命令）。
- 队列状态本身可观测：`queue_status.py` 会把「排队中（第 N 位）」渲染成一种 status。

**本项目的真实痛点**：`bot/handlers.py::_spawn_sync` 是无条件 `asyncio.create_task`，
库里只有 `asyncio.Semaphore` 用于**相册内并发下载**，**没有任何任务级并发上限**。
而 `RateLimiter` 是**每个 Syncer 实例一个**，所以同时起 3 个克隆时，实际频率是设计值的 3 倍，
UserBot 的 FloodWait 会以乘积速度到来——这正好解释了「多任务同时跑的时候封控特别快」。

**改法**（约 120 行，新增 `core/task_queue.py`）：

```python
class TaskQueue:
    """克隆/监听任务的全局限流与排队。"""

    def __init__(self, limit_all: int = 1, limit_per_source: int = 1):
        self._limit_all = max(1, limit_all)
        self._limit_per_source = max(1, limit_per_source)
        self._running: set[int] = set()
        self._by_source: dict[int, set[int]] = defaultdict(set)
        self._waiters: dict[int, asyncio.Event] = {}
        self._order: list[int] = []          # 排队顺序，用于显示「第 N 位」

    async def acquire(self, task_id: int, source_id: int, *, force: bool = False):
        while True:
            if force or self._can_start(source_id):
                self._running.add(task_id)
                self._by_source[source_id].add(task_id)
                self._waiters.pop(task_id, None)
                self._order = [t for t in self._order if t != task_id]
                return
            ev = self._waiters.setdefault(task_id, asyncio.Event())
            if task_id not in self._order:
                self._order.append(task_id)
            await ev.wait()                  # 被放行时由 release() 唤醒

    def release(self, task_id: int, source_id: int):
        self._running.discard(task_id)
        self._by_source.get(source_id, set()).discard(task_id)
        self._wake_next()

    def _can_start(self, source_id: int) -> bool:
        if len(self._running) >= self._limit_all:
            return False
        return len(self._by_source.get(source_id, ())) < self._limit_per_source

    def position(self, task_id: int) -> int | None:
        return self._order.index(task_id) + 1 if task_id in self._order else None
```

配套改动：

- `tasks.status` 增加 `queued` 状态；`get_tasks_for_list()` 的排序把 `queued` 放在
  `running` 之后。
- `bot/menu.py::render_tasks` 对 `queued` 显示「⏳ 排队中（第 2 位）· 前面 1 个任务」。
- `_spawn_sync` 里在 `syncer.start_sync` 前 `await queue.acquire(...)`，
  `finally` 中 `queue.release(...)`；或者更稳的做法是把 acquire 放进 `Syncer.start_sync`
  的开头，避免 spawn 阶段被卡住时任务状态与实际不符。
- 配置：`clone.max_concurrent: 1`、`clone.max_concurrent_per_source: 1`（默认 1 就够，
  单 UserBot 场景并发只会更快触发限流，不会更快完成）。

验收口径：同时下 3 个 `/kl`，只有 1 个 `running`、2 个 `queued`，且 `/kllb` 显示排队位次；
前一个跑完后第二个自动转 `running`（不需要人工点「继续」）。

#### ② 状态适配器 + 注册表 + 统一分页渲染

**它的做法**：`status_utils/` 下 20 个 `XxxStatus` 类，全部实现同一组方法
（`gid / name / size / processed_bytes / progress / speed / eta / status / eng`），
`download_dict[message.id] = XxxStatus(...)`，渲染器遍历字典 + `STATUS_LIMIT` 分页。

**本项目现状**：`render_tasks` / `render_monitors` / `render_task_detail` 三处各自拼字符串，
克隆与监听的进度显示逻辑（百分比 vs 条数、速率、剩余时间）在两处重复实现，
`get_tasks_for_list(types, limit=12)` 是硬截断而不是分页。

**改法**：定义一个最小协议，三种任务各实现一个 `StatusView`：

```python
class StatusView(Protocol):
    def headline(self) -> str: ...        # 「📥 频道克隆 #12」
    def metrics(self) -> list[str]: ...   # ["进度: ▓▓▓░░ 62%", "速率: 120 条/分"]
    def actions(self) -> list[list[Button]]: ...
    def sort_key(self) -> tuple: ...
```

`render_tasks` 只负责「取 view 列表 → 排序 → 切片 → 拼」，不再关心是克隆还是监听。
收益：新增任务类型（比如以后要做「批量导出」）不用碰渲染层；顺手就能加 `page:N` 翻页回调，
替掉现在的 12 条硬截断。

#### ③ Config 类即 schema（顺带修掉每次读盘）

**它的做法**：`bot/core/config_manager.py` 里 `Config` 的类属性就是配置项 + 类型默认值，
`_convert()` 按默认值类型强转（bool 认 `true/1/yes`，list/dict 走 `literal_eval` 并校验），
`_load_from_module()` / `_load_from_env()` 双来源，`set()` 支持运行时热改，
`bot_settings.py` 用按钮改配置并落 Mongo。

**本项目现状**（已核实的两个问题）：

- `core/rate_limiter.py::wait()` **每次调用都 `yaml.safe_load(open("config.yaml"))`** ——
  克隆 10 万条 = 10 万次磁盘 I/O，虽然被 page cache 挡住，但仍是纯浪费。
- `config` 是裸 dict，键名写错只在运行时 `KeyError`，没有默认值兜底，没有类型校验。
- `/settings` 是**只读**的（只展示限流配置），改配置必须改文件 + 重启。

**改法**（`core/config.py`，约 100 行）：

```python
class Config:
    SCHEMA = {
        "clone.batch_size": (int, 200),
        "clone.max_concurrent": (int, 1),
        "clone.follow_after_finish": (bool, True),
        "rate_limit.forward_interval": (list, [1, 2]),
        "transfer.userbot_forward": (bool, True),
        "transfer.probe_ttl_seconds": (int, 1800),
    }

    @classmethod
    def load(cls, path="config.yaml"):        # 进程启动时调一次
        raw = yaml.safe_load(open(path)) or {}
        cls._data = {k: cls._convert(k, cls._dig(raw, k)) for k in cls.SCHEMA}

    @classmethod
    def get(cls, path):                        # 热路径：纯内存
        return cls._data.get(path, cls.SCHEMA[path][1])

    @classmethod
    def set(cls, path, value, *, persist=True):  # 菜单改配置
        cls._data[path] = cls._convert(path, value)
        if persist:
            cls._write_back(path, cls._data[path])
```

要点：
- **保留现有 yaml 的嵌套结构**（不要学项目 1 的扁平大写环境变量风格），只加一层点路径
  访问 + 类型表，迁移成本低。
- `RateLimiter.wait()` 改读 `Config.get(...)`，热路径零 I/O。
- `/settings` 从只读升级为可改（`Config.set()` 后写回 yaml，并让正在跑的任务用新值）。
- `Config.set` 的持久化也可以落到 SQLite（`settings` 表），避免容器重建丢配置——
  这一点比项目 1 存 Mongo 更适合你。

#### ④ 重启后主动通知，而不是只写日志

**它的做法**：`db_handler.add_incomplete_task(cid, link, tag)` 把未完成任务写进
`tasks[bot_id]` 集合，启动时 `INCOMPLETE_TASK_NOTIFIER` 读出并向对应 chat 发
「重启前有 N 个任务未完成，已恢复」。

**本项目现状**：`_auto_resume_clones()` 恢复成功只 `logger.info("已自动续传 %d 个克隆任务")`，
用户在群里完全无感——容器被 watchtower 重建后，用户会以为任务丢了。

**改法**（约 20 行）：恢复循环里对每个成功的任务，向 `t["notify_chat_id"] or target_chat_id`
发一条：

```
♻️ 容器重启，任务已自动续传
• 任务 #12 · 源：xxx
• 从游标 msg_id=8421 继续，已克隆的不会重复
```

失败的那些已经是 `paused` 了，同样发一条带「▶️ 继续」按钮的提示。

#### ⑤ 两层权限 + 声明式过滤器

**它的做法**：`CustomFilters.sudo / owner / authorized` 挂在 handler 定义上，
权限不进函数体；`sudo_users` / `auth_chats` 从 DB 读，可在运行时增删。

**本项目现状**：26 个 handler 里有 25 处手写 `if not await _require_admin(event): return`，
且只有「管理员 / 非管理员」一档，没有 owner 与授权用户的分层。

**改法**：保留 `_require_admin` 作为底层实现，外面包一层装饰器：

```python
def admin_only(func):
    @wraps(func)
    async def wrapper(event, *a, **kw):
        if not await _require_admin(event):
            return
        return await func(event, *a, **kw)
    return wrapper

@bot.on(events.NewMessage(pattern=r"^/kl(\s|$)"))
@admin_only
async def on_kl(event): ...
```

顺手把「角色 → 权限」抽成 `ROLES = {"owner": {...}, "admin": {...}, "viewer": {...}}`，
为以后「只读用户」留位。这一条和上一轮报告里的 P1「全局错误兜底装饰器」可以合并成
一个 `@guard(admin=True)`，一次改完。

---

### 3.2 来自 KPSML-X

#### ① 声明式参数解析（直接替掉手写正则）

**它的做法**（`bot_utils.py::arg_parser`）：

```python
arg_base = {'link': '', '-i': 0, '-up': '', '-upload': '', '-rcf': ''}
# 输入："/clone -i 3 -up myfolder https://t.me/x"
arg_parser(items, arg_base)
# → {'link': 'https://t.me/x', '-i': '3', '-up': 'myfolder', ...}
```

`arg_base` 的键就是 schema，值的位置自由、flag 可出现在链接前后，新增一个参数只改一行。

**本项目现状**：`parse_clone_args` + `parse_kljx_args` 两套手写正则（`_RANGE_RE`、
`_SINGLE_ID_TAIL`），要处理「区间在链接前」「区间在链接后」「数字结尾的链接不能被误吞」
这些边界，测试里为此写了十几个用例。

**改法**：借鉴 schema 思路，但**不要照抄它的实现**（它不做类型校验、bool flag 靠硬编码
`{'-b','-e','-z','-s','-j'}` 判断、值不支持含空格）。建议写成带类型的版本：

```python
ARG_SPEC = {
    "-from": ("int", None),      # 起始消息 ID
    "-to":   ("int", None),      # 结束消息 ID
    "-min":  ("size", None),     # 10M / 2G
    "-max":  ("size", None),
    "-link": ("flag", False),    # 只要含链接的
    "-nolink": ("flag", False),
    "-force": ("flag", False),   # 跳过队列插队
}
```

保留现有 `/kl 100-1100 <链接>` 的位置式区间语法作为兼容层（用户已经习惯了），
内部转成 `-from/-to` 同一套结构。这样 `/kljx <链接> 120`、`/kl <链接> -min 10M -nolink`
共用一条解析路径，测试从「十几个边界用例」收敛成「spec 驱动的一组用例」。

#### ② 主题层：把 506 处文案收进一个模块

**它的做法**：`BotTheme('KEY', **kwargs)` 从主题模块取模板并 `format`，主题包
`kpsml_minimal.py` 12KB 全是文案；换皮肤 = 换一个主题文件。

**本项目现状**（已统计）：`bot/handlers.py` 里 **329** 处含中文的字符串字面量，
`bot/menu.py` 里 **177** 处，合计 **506** 处。改一句文案要 grep 全仓库，而且
`/help`、菜单「❓ 命令与说明」、`main.py::BotCommand` 三处的命令表各写了一遍（已经在
上一轮出现过三处不同步的问题）。

**改法**（`bot/texts.py`，纯字符串常量 + 少量 `format` 模板）：

```python
# bot/texts.py
HELP = """【克隆】
/kl <链接> — 克隆全部消息到本群，完成后自动持续监听
/kl {a}-{b} <链接> — 只克隆 msg {a}~{b}（含两端）
...
"""

HELP_QUEUED = "⏳ 排队中（第 {pos} 位）· 前面还有 {ahead} 个任务"
ERR_NO_PERM = "⛔ 无权限：只有管理员可以使用该命令"

def task_detail(t: dict) -> str:
    ...  # 复杂模板留函数，简单文案用常量
```

配合「命令目录单一来源」：定义一次 `COMMANDS = [(name, args, desc, handler), ...]`，
`/help`、菜单说明、`main.py::BotCommand` 三处**都从它生成**。这条和主题层一起做，
才能根治「三处不同步」。

#### ③ 每用户任务上限（可以先于完整队列落地）

`get_user_tasks(user_id, maxtask)` + `getAllDownload(status, user_id)` 的用法很简单：
超过上限直接回「你已有 N 个任务在跑，先等一个完成」。

如果你的机器人实际上只有你自己（或少数几个管理员）在用，**先做这一条比做完整队列便宜**：
一个 `MAX_TASKS_PER_CHAT = 2` 的计数检查（查库即可，无需新模块）就能挡住 90% 的
「一次点五个克隆」场景。等真需要公平排队再上 3.1① 的完整队列。

#### ④ 健康检查：进程假死目前发现不了

**本项目现状**（已核实）：`Dockerfile` 与 `docker-compose.yaml` 里**没有 HEALTHCHECK**。
watchtower 只看镜像有没有更新，不会重启「进程还在、但 Telethon 已经卡死不再收消息」的容器。
监听任务最怕这种状态：列表显示 `running`，实际一条都不转。

**改法**（借鉴 `alive.py` 的思路，但用更轻的方式）：

```python
# core/heartbeat.py
HEARTBEAT = Path("/app/data/.heartbeat")

async def beat_forever():
    while True:
        HEARTBEAT.write_text(str(time.time()))
        await asyncio.sleep(30)
```

```yaml
# docker-compose.yaml
healthcheck:
  test: ["CMD-SHELL", "python -c \"import time,sys; \
         sys.exit(0 if time.time()-float(open('/app/data/.heartbeat').read())<120 else 1)\""]
  interval: 60s
  timeout: 10s
  retries: 3
  start_period: 90s
```

心跳由**真正在处理消息的循环**打点（比如 `MonitorManager` 每轮刷新统计时、`Syncer`
每批处理完时），而不是独立定时器——独立定时器在循环卡死时照样打点，等于没检测。

---

### 3.3 来自 tg-message-clone

> 再次说明：该仓库只公开了过滤器子集，以下仅评价可见代码。**思路可取，实现不要照抄**。

#### ① `frozen_config` 配置快照：任务开始后参数不再变

它的每个过滤器函数都接收 `frozen_config`（任务启动时冻结的用户配置），而不是运行时读全局。
好处：跑一个大任务时你改设置，**不会让同一任务的前半段和后半段用不同规则**。

**本项目现状**：`Syncer.__init__` 里已经读了 `batch_size` / `progress_notify_every`
（构造时快照，这点是对的），但 `mode`、`follow`、限流参数、转发策略开关都是运行时读全局。
重启续跑时如果配置变了，同一任务的行为会前后不一致。

**改法**：定义 `CloneOptions`（frozen dataclass），建任务时算好、序列化进
`tasks.options_json` 列，续跑时反序列化。收益：可复现、可回放，也让「为什么这条用了
下载而那条用了转发」有据可查。

```python
@dataclass(frozen=True)
class CloneOptions:
    mode: str                 # copy / forward
    follow: bool
    batch_size: int
    from_id: int = 0
    to_id: int = 0
    min_size: int = 0
    max_size: int = 0
    allow_download: bool = True   # 是否允许降级到「下载+上传」
```

#### ② 单一 `should_skip` + 有序规则链 + 跳过原因

它的 `should_skip_message` 返回一个 bool，规则按「便宜 → 贵」排列。**但它的短板是
只有 bool、没有原因**，所以界面上只能说「跳过 12 条」，说不出为什么。

**本项目现状**：`core/message_logic.py` 里 `is_file_media` / `is_album_candidate` /
`classify_message_kind` / `detect_hard_restriction` / `is_restricted_message` 分散判断，
`syncer.py` 里 `counter["skip"]` 只有一个总数。

**改法**：合并成一个返回原因的判定函数，原因直接进统计：

```python
def should_skip(msg) -> tuple[bool, str]:
    if isinstance(msg, MessageService):
        return True, "service"
    if not msg.text and not msg.media:
        return True, "empty"
    if _is_giveaway(msg.media) or _is_unsupported(msg.media):
        return True, "unsupported"
    if opts.max_size and msg.file and msg.file.size > opts.max_size:
        return True, "too_large"
    return False, ""
```

`skip_reasons: Counter[str]` 落库，任务详情显示「跳过 12 条（服务消息 9 · 超大 3）」。
这比项目 1 的 status 类更轻，收益立刻可见：以后「为什么少了几条」不用再翻日志。

#### ③ 人类友好的大小输入（顺带补一个功能缺口）

它的 `parse_size_to_bytes("1.5G")` 支持 `B/K/KB/M/MB/G/GB/T/TB`，空上界语义是 `inf`，
`get_file_size_range` 返回 `(min, max, is_active)` 三元组——**用一个三元组同时表达
「未启用」「单边限制」「双边限制」**，比用 `None` 满天飞干净。

**本项目现状**：克隆只能按消息 ID / 时间范围，无法「只克隆小于 2G 的」「跳过纯文本」。
配合 3.2① 的 `-min/-max/-link` flag，正好补上这个功能缺口，而且解析函数可以几乎照搬
（这部分它写得没问题）。

#### ④ 规则覆盖语义：后写的生效

`process_links_filter` 用 `rfind` 找**最后一次**出现的关键词，所以
「包含链接，但最后还是要排除链接」→ 排除生效。这是符合 CLI 直觉的覆盖语义。

**本项目现状**：`parse_clone_args` 是「第一个匹配生效」。如果做成 flag 体系，
建议明确「同名 flag 后者覆盖前者」，并在 `/help` 里写一句。

---

## 4. 差距汇总

| 能力 | 本项目现状 | 最佳参考 | 建议优先级 |
| --- | --- | --- | --- |
| 任务并发控制 / 排队 | **无**，`create_task` 直接起 | mirror-leech 队列 | **P0** |
| 健康检查 / 假死检测 | **无 HEALTHCHECK** | KPSML-X `alive.py` | **P0** |
| 配置读取与校验 | 每次 `wait()` 读 yaml，裸 dict | mirror-leech `Config` | **P0** |
| 重启后用户可见 | 只写日志 | mirror-leech 未完成通知 | P1 |
| 状态渲染 | 3 处各写一套，12 条硬截断 | mirror-leech 适配器+分页 | P1 |
| 命令参数解析 | 2 套手写正则 | KPSML-X `arg_parser`（需加类型） | P1 |
| 用户文案 | 506 处字面量，命令表三处重复 | KPSML-X themes | P1 |
| 权限 | 25 处重复 `if not admin` | mirror-leech 过滤器 | P1 |
| 跳过原因可观测 | 只有总数 | tg-message-clone 规则链（需加 reason） | P2 |
| 任务参数可复现 | 部分运行时读全局 | tg-message-clone `frozen_config` | P2 |
| 消息大小/链接过滤 | 无 | tg-message-clone | P2 |
| 持久化 | **SQLite + WAL + 备份（优于三者）** | — | 保持 |
| 转发降级链 | **四层降级 + 能力缓存（优于三者）** | — | 保持 |
| 单元测试 | **172 例（三者皆无）** | — | 保持 |

---

## 5. 落地顺序建议

### P0（本机稳定性，建议一起做）

1. **任务队列**（`core/task_queue.py`，~120 行 + 3 处接线）——见 3.1①
2. **健康检查**（`core/heartbeat.py` + Dockerfile/compose HEALTHCHECK，~30 行）——见 3.2④
3. **Config 单次加载 + 类型校验**（`core/config.py`，~100 行，替换 `rate_limiter` 的
   `yaml.safe_load`）——见 3.1③
4. 上一轮报告里的 **SIGTERM 优雅停机** 与 **全局错误兜底** 仍属 P0，尚未落地

### P1（可维护性）

5. 命令目录单一来源（`/help` / 菜单 / `BotCommand` 三处 → 一处生成）
6. `bot/texts.py` 文案层（506 处字面量逐步收敛，先收 `/help` 与错误提示）
7. 参数解析 spec 化（保留位置式区间做兼容）
8. 状态适配器 + 分页（替掉 12 条硬截断）
9. 权限装饰器合并到 `@guard(admin=True)`

### P2（功能与可观测性）

10. `skip_reasons` 分解 + 任务详情展示
11. `CloneOptions` 快照落库
12. `-min/-max/-link` 过滤
13. 重启通知（其实更像 P1，20 行的事）

---

## 6. 不建议照搬的部分

| 不学 | 原因 |
| --- | --- |
| mirror-leech 的 `helper/common.py`（53KB 上帝类） | 与本项目「拆成 `core/forwarder.py` + `core/syncer.py`」的现状相比是倒退 |
| mirror-leech 的全局可变状态（`from .. import download_dict`） | 本项目用构造注入（`MenuController(..., toggle_follow=...)`）已经把依赖关系显式化，别退回隐式全局 |
| KPSML-X 的 `update.py`（容器内 `git reset --hard`） | 与 CI→镜像→watchtower 的不可变部署冲突；镜像内容会漂移，回滚失效 |
| KPSML-X `arg_parser` 的实现细节 | 无类型校验、bool flag 硬编码、值不支持空格；只学 schema 思想 |
| tg-message-clone 的代码风格 | `print` + ANSI 转义、`await` 同步函数（真 bug）、`frozen_config=None` 却直接下标、大量注释死代码、零测试 |
| MongoDB | 单机部署下 SQLite + WAL + 备份脚本已经更合适，引入外部依赖是净负担 |
| uvloop | 理论上 5-15% 的 asyncio 开销收益，但需实测与 Telethon/aiosqlite 的兼容性；收益远小于上面几项，放最后考虑 |
