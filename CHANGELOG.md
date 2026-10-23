# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed（转发策略：私有频道不再下载）
- **统一降级链**（`core/forwarder.py`）：公共频道与私有频道走同一顺序，逐级降级
  `① Bot 直接克隆 → ② UserBot 转发 → ②b UserBot 读+Bot 写 → ③ 下载+上传 → ④ 失败标记`。
  - 新增 **策略②（`_try_userbot_forward` / `_try_userbot_forward_album`）**：由 UserBot
    调用 `forward_messages` 直接搬运，`copy` 模式用 `drop_author=True` 去掉来源标记。
    服务端搬运，不落地、不受上传体积上限约束，1GB 视频从「几分钟」降到「一次 RPC」。
  - 原「UserBot 读 + Bot 写」下移为 **策略②b**（`_try_userbot_copy_via_bot*`），
    仍是"不下载"的兜底，用于 UserBot 能读源但进不了目标的情况。
  - `_forward_messages_drop_author` 泛化为 `_native_forward(client, ...)`，Bot/UserBot 共用，
    论坛话题的 `top_msg_id` 注入对两者都生效。
  - **边界**：私聊目标（`caller="private"` 或 `target_chat_id > 0`）仍走 Bot 发送，
    保留原有的 >300MB 视频切片规则，不改变私聊解析的既有观感。
- **能力判定缓存**（`transfer.probe_ttl_seconds`，默认 1800s）：
  「Bot 读不到该源」`_bot_unreadable`、「UserBot 发不进该目标」`_ub_write_blocked`、
  「源禁止转发」`_noforwards` 判定一次即复用。私有频道克隆不再逐条撞
  `ChannelPrivateError` / `ValueError`（几千条就是几千次无效 RPC），这也是提速的主要来源之一。
- **UserBot 风控**（用户账号代价远高于 Bot）：
  - `RateLimiter.wait(kind="userbot")`：独立节奏 `userbot_forward_interval`（默认 3–6s）
    与批次长歇 `userbot_forward_pause_every: 25` / `userbot_forward_pause_time: [60,120]`，
    命中策略②时**替换**通用间隔而非叠加；
  - `_handle_userbot_flood`：FloodWait 额外多等 `userbot_flood_buffer`（默认 5s），
    连续 `userbot_max_flood`（默认 3）次后**本次运行熔断策略②**，改走 Bot/下载路径；
  - 源 `noforwards` 直接跳过策略②，不做注定被拒的尝试。
- **可观测**：`Forwarder.strategy_counts` 统计各层命中次数，克隆完成消息新增
  「策略分布: Bot直接 N · UserBot转发 M · …」一行；日志区分 `策略2` / `策略2b`。
- **配置**（`config.example.yaml`）：`transfer.userbot_forward`、`userbot_copy_fallback`、
  `userbot_max_flood`、`userbot_flood_buffer`、`probe_ttl_seconds`；
  `rate_limit.userbot_forward_*` 三项。
- **行为差异（注意）**：`copy` 模式在策略②下由 **UserBot** 发帖（`drop_author` 只去掉
  「转发自」）。要求署名必须是 Bot 时设 `transfer.userbot_forward: false`。
- 文档：README / README.en 重写「转发策略」为分层表（含成本、适用条件、身份变化）；
  `docs/clone.md` 新增「转发策略：私有频道不再下载」「风控：怎么把 UserBot 的风险压到最低」
  两节与对应 FAQ；`docs/architecture.md` 设计原则补充例外。
- 测试：新增 `test/test_forward_strategy.py`（16 例）——层级命中、copy/forward 的
  `drop_author`、UserBot 不能写目标时回退②b、两层都失败走③、能力缓存（Bot 与目标两侧）、
  noforwards 跳过、`userbot_forward: false`、私聊目标不走 UserBot、FloodWait 熔断与禁用后
  不再调用、RateLimiter 的 userbot 计数、策略摘要渲染。

### Added
- **克隆记录永久保留**：`completed` 的克隆任务不再从列表消失。
  - 列表查询改为 `status != 'deleted'`，并按 `running → paused → failed → 其它` 排序，
    已完成的克隆始终可见（`db/models.py: get_tasks_for_list`）
  - 删除与「清空」改为**软删除**（`status='deleted'`），游标、`message_map` 映射、
    完成时间都留在库里，误删可手工把状态改回 `paused` 复活
  - `/kllb`、主菜单「📋 克隆任务列表」都基于这份查询
- **跑完自动转持续监听**：克隆任务完成时 `syncer.on_complete` → `_on_clone_completed`
  → `_ensure_follow_monitor`，自动建一个 `monitor` 任务接管后续新消息，
  并用 `parent_id` 指回来源克隆任务；监控列表显示「（来自克隆任务 #N）」。
  `clone.follow_after_finish: false` 可全局关闭。
- **持续监听开关**：任务详情新增 `🛰 持续监听: 开启（监听任务 #N）/ ⏹ 关闭` 与
  `follow:<id>` 按钮（`_toggle_follow`）。关闭只是暂停对应 monitor 任务并清 `follow` 标记，
  记录与已转发计数都保留，再点一次即恢复。
- **消息 ID 区间克隆**：`/kl 100-1100 <链接>`，`tasks.end_msg_id` 落库，
  `iter_messages` 用 `max_id = end_msg_id + 1` 保证**两端都含**；进度描述写作「→ 到消息 N 为止」。
- **新命令族**（`bot/handlers.py`，`/help` 同步改写为命令↔功能对照）：
  | 命令 | 作用 |
  | --- | --- |
  | `/kl <链接>` | 克隆全部到本群，完成后持续监听 |
  | `/kl 100-1100 <链接>` | 只克隆该 ID 区间（含两端） |
  | `/kljx <链接>` | 继续克隆：`find_clone_task` 找同源同目标最新记录 → `reopen_clone_task` → 从游标续跑，不重复；先比对源最新消息（`latest_source_msg_id`），已追平就回「已是最新」不空跑，查不到才按无法判断处理（不会误判成已追平） |
  | `/kljx <链接> 120` / `120-1000` | 显式改写续跑点：`set_clone_resume_point` 把游标设为 `ID-1`、`start_msg_id=ID`（区间再设 `end_msg_id`），跳过「已是最新」判断从该条重克隆（`parse_kljx_args` 支持 ID 写在链接前或后） |
  | `/jt <链接>` | 只监听新消息，先校验 UserBot 已加入源 |
  | `/kllb` | 克隆任务列表 |
  | `/jtlb` | 监听任务列表 |
  | `/chapd` | 等一条转发的频道消息，返回频道名 / @用户名 / ID / 链接与现成命令（10 分钟 TTL，`describe_forwarded_source`） |
  | `/chaid` | 等一条转发的频道消息，返回源频道里的**消息 ID**（`fwd_from.channel_post`）与 `t.me` 消息链接（`extract_forwarded_msg_id` + `build_message_link`），并附现成的 `/kljx <链接> <ID>` 命令；转发个人消息/复制文本时给出明确告警 |
- `bot/menu.py`：主菜单改为「👁 新建持续监听 / 📥 新建克隆任务 / 📋 克隆任务列表 /
  📡 监听任务列表 / ⚙️ 限流设置 / ❓ 命令与说明」；列表标题显示「运行中 N · 持续监听 M」，
  持续监听的任务行带 `🛰` 标记；已完成克隆在详情页提供「🔁 继续补新消息」（`cont:<id>`）。
- `config.example.yaml`：新增 `clone.follow_after_finish: true`。
- `test/test_clone_follow.py`（44 个用例）：命令参数解析（含 `parse_kljx_args` 的
  ID 前后置与"链接以数字结尾"边界）、转发来源识别、消息 ID/链接提取（`/chaid`）、
  新列与老库迁移、已完成记录可查与 `reopen_clone_task`/`set_clone_resume_point`、
  区间克隆的 `max_id` 边界、详情页按钮与 `follow:`/`cont:` 分发、
  `/chapd` `/chaid` 提示与识别结果、`latest_source_msg_id` 的多客户端回退与"查不到返回 None"。

### Fixed
- **「实时监控新消息」建了却不生效、且没有任何提示**：
  - 根因：`events.NewMessage(chats=<源>)` 在 UserBot 无法解析该实体（私有频道、未缓存）时
    **静默收不到任何更新**。改为注册 `events.NewMessage()` 全量接收，再用 `_match_chat`
    手工比较 `event.chat_id`（拿不到 `chat_id` 时退回 `message.peer_id`，仍无法判定则**放行**，
    宁可多判一次也不静默漏转）。
  - 创建失败不再只弹一个转瞬即逝的 toast：`event.answer(alert=True)` + 原地改写消息，
    写明失败原因、常见原因和「重试」按钮。
  - 向导中非管理员的操作从静默丢弃改为明确回复「⛔ 无权限」。

### Changed
- `/list` 与菜单列表统一走 `get_tasks_for_list`，克隆与监控混排时仍能一眼看出类型与状态。
- `_resume_clone(task_id, allow_completed=False)`：允许从 `completed` 状态续跑，供 `/kljx` 与
  「🔁 继续补新消息」使用。

### Added（更早一批：自动更新与数据持久化）
- **NAS 自动更新链路**（`docker-compose.yaml` 内置 `watchtower`）：CI 推送 `:latest` 后，
  群晖上的 watchtower 每 10 分钟检查一次 digest，有新镜像就优雅停止并原地重建 bot，
  不再需要 SSH 进去手动 `docker compose pull`。使用维护中的 fork
  `ghcr.io/nicholas-fedor/watchtower:1.19.0`（上游 containrrr 已于 2025-12 归档），
  固定版本避免它升级自己，`WATCHTOWER_LABEL_ENABLE` 只作用于带
  `com.centurylinklabs.watchtower.enable=true` 的 bot 容器，`WATCHTOWER_CLEANUP` 清理旧镜像。
  文档 `docs/auto-update.md`：原理链路、参数、私有包认证、回滚、与群晖「自动更新」的差异、排障。
- **数据落盘位置可配 + 持久化自检**（`core/storage.py`）：数据库路径支持
  `TG_DB_PATH` > `config.yaml` 的 `storage.db_path` > 默认 `data/bot.db`；
  启动时打印绝对路径/大小/是否位于宿主机挂载目录，不在挂载目录内时输出带修复步骤的
  `WARNING`（`storage.persist_dirs` / `TG_PERSIST_DIRS` 可覆盖判定目录）。
- **SQLite 在线备份**（`db/backup.py`、`scripts/backup_db.py`、菜单「💾 备份数据库」）：
  用 backup API 生成一致性快照到 `data/backups/bot-YYYYmmdd-HHMMSS.db`，
  默认保留最近 7 份（`storage.backup_keep`），随后给出可直接执行的恢复步骤；
  也可 `docker compose exec bot python scripts/backup_db.py --keep 14` 或挂群晖任务计划。
- **WAL 与写保护**：`Database` 默认 `journal_mode=WAL` + `synchronous=NORMAL` +
  `busy_timeout=5000` + `foreign_keys=ON`，容器被 stop/kill、升级重建时已提交进度不丢、
  库不损坏；正常退出自动 `wal_checkpoint(TRUNCATE)`。SMB/NFS 场景可用 `storage.wal: false` 关闭。
- 「⚙️ 限流设置」页新增「💾 数据存储」区块，直接显示数据库路径、大小与持久化状态。
- `docs/storage.md`：数据清单、群晖挂载步骤、自定义路径、备份/恢复/迁移、常见报错。
- `test/test_db_persistence.py`：路径优先级、持久化判定、WAL 开关、备份内容与保留策略、
  菜单备份入口共 19 个用例。
- **监控任务进度与防重复**：`/monitor` 与菜单创建的监控任务现在会
  - 记录并展示进度：`done_count` / `fail_count` / `skip_count`、最近处理的消息 ID、
    最近活动时间与最近错误；内存累加、每 5 秒批量落库（`MONITOR_STATS_FLUSH_SECONDS`），
    暂停/删除/源不可访问时立即落盘
  - 主菜单新增「📡 监控任务（已配置）」总览，逐条显示源（含话题范围）、目标、
    已转发/失败/跳过、最近消息与最近活动；详情页、`/list`、`/m:task` 同步展示，
    暂停状态显示「▶️ 继续监控」
  - **创建即确认**：成功后发送一条含任务号、源、目标、模式、监听范围的消息，
    带「详情 / 监控任务 / 暂停」按钮，不会再出现"点了没反应、以为没建成功"的情况
  - **重复检测**：同一「源话题 → 目标话题」已有 running/paused 的监控时不再新建，
    返回已有任务号；暂停中的直接提供继续按钮，避免同一消息被转发两遍
- **按钮式菜单交互**（`bot/menu.py`）：`/start`、`/menu` 打开图形化主菜单，用按钮向导完成
  「新建克隆 → 选源 → 选目标 → 选起点 → 选模式 → 确认」，以及「任务列表 / 进度详情 /
  暂停 / 继续 / 删除 / 设置 / 帮助」。向导状态 15 分钟 TTL，命令式用法全部保留。
- **克隆起点可选**：从头（最早 → 最新）、从指定消息 ID（含该条）、从指定时间（自动换算
  消息 ID 边界）、继续上次进度。命令侧对应 `/sync <链接> --from-id N` 与
  `/sync <链接> --from-time 2024-01-31[T12:30]`。
- **进度记录**：`tasks` 表新增 `start_msg_id`、`start_date`、`total_count`、`done_count`、
  `fail_count`、`skip_count`、`source_title`、`target_title`、`last_error`、`notify_chat_id`、
  `notify_msg_id`、`created_by`、`finished_at`；每处理一个转发单元即落盘游标与计数。
  进度消息原地编辑刷新（带进度条 / 百分比 / 速率 / 剩余时间），不刷屏。
- **断点续传**：暂停、进程崩溃、容器重启后均从 `last_synced_msg_id` 严格继续，不重复不遗漏；
  启动时自动续传被中断的任务，可用 `clone.auto_resume: false` 改为仅置为暂停。
- `db/database.py` 自动迁移：比对 `PRAGMA table_info` 后 `ALTER TABLE ADD COLUMN` 补齐新列，
  老库升级不丢数据、可重复执行。
- `core/clone_plan.py`：时间解析、进度条渲染、耗时估算、**相册安全分批**等纯逻辑工具。
- `docs/clone.md`：克隆功能说明（起点语义、进度字段、续传机制、FAQ）。

### Changed（更早一批）
- `docker-compose.yaml`：补充挂载注释（`/app/data` 即数据库所在）、可选的独立 db 目录挂载示例、
  `stop_grace_period: 20s`，并说明不要直接挂载单个 `bot.db` 文件。
- `main.py` 启动流程改为先解析路径再建目录/连库，退出路径统一关闭数据库。
- `core/syncer.py` 重写为**流式分批**处理（`clone.batch_size`，默认 200）：不再把整个频道的
  消息一次性读进内存，几万条的大频道也能稳定跑；相册不会被切到两批；服务消息与空消息计入
  `skip_count` 并推进游标，不会卡住；FloodWait 后自动从游标重试（最多 5 轮）。
- `/sync` 与菜单共用同一条创建链路，因此命令方式也获得了进度消息与断点续传能力。
- 目标总量改用服务端 `count`（`min_id` 过滤），进度百分比是真实值而非估算。

### Added (continued)
- MTProto 代理自动选优：每小时拉取 telegram-mtproto-proxy-list 的 `top40.json`（多镜像回退），
  在本机对每条代理做真实 FakeTLS 握手测速后选用最快可用代理，写入 `data/proxy.json`；
  代理失效或明显更优时自动切换（`switch_improve_ratio` 控制切换阈值）。
  `config.yaml` 新增 `proxy` 配置段（`off` / `auto` / `manual`），默认 `off` 保持直连行为。
- `ConnectionTcpMTProxyFakeTLS`（`core/faketls_mtproxy.py`）：为 Telethon 补齐 FakeTLS
  （`0xEE` 型 secret）MTProto 代理支持——TLS 1.3 ClientHello 伪装、服务端整包 HMAC 校验、
  TLS 记录封装、padded intermediate 传输。
- `core/mtproxy_probe.py`：纯标准库 FakeTLS 握手测速引擎（源自上游 proxy-list 项目）。
- `core/proxy_manager.py`：榜单拉取 / 本地测速 / **DC 往返校验** / 选优 / 状态文件 /
  Telethon 集成，并提供 `python -m core.proxy_manager --test|--once` 命令行工具。
  新增 `probe.dc_validate_top`、`probe.dc_validate_timeout`：对握手排序前 N 名再发一次
  明文 `req_pq_multi`，只有拿到 nonce 匹配的 `resPQ` 才算真正可用，避免选中「能握手但
  传不了数据」的假活代理。
- `core/keepalive.py`：连接保活补丁。Telethon 的 `_keepalive_loop` 心跳硬编码 60 秒，而公共
  MTProto 代理普遍在 45~55 秒空闲后断连，导致空载连接周期性被掐断并重连；补丁把心跳改为
  可配置（`proxy.keepalive_seconds`，默认 20 秒），修复日志中反复出现的
  `Server closed the connection: 0 bytes read on a total of 4 expected bytes`。
- `core/proxy_manager.create_client()`：客户端构造的唯一入口，强制注入 `connection`/`proxy`；
  配套 `verify_proxy_applied()` 校验代理实际生效（含已建立连接的真实传输类）。
  Bot、UserBot、登录脚本全部改为经此构造，杜绝新增代码漏传代理导致的直连失败。
- `scripts/audit_proxy_egress.py`：出网审计脚本——在 socket 层记录每一次真实建连，逐条触发
  链接解析 / 历史同步 / Takeout / 跨 DC 导出 / 媒体下载 / 4 连接并发，最后核对目标地址是否
  全部等于代理服务器；出现 Telegram 官方网段即判 FAIL。
- `docs/proxy-audit.md`：逐功能代理覆盖核对表（链接解析、历史同步、受限消息补发、实时监控、
  多级降级、相册感知、硬封禁过滤、双模式、并发加速、首次登录）与自证方法。
- `test/test_proxy_coverage.py`：静态守卫——除 `core/proxy_manager.py` 外任何文件直接构造
  `TelegramClient` 都会让测试失败。
- `test/test_faketls_local.py`：内置 Mock FakeTLS 代理服务端，覆盖「握手 → 记录封装 →
  混淆头校验 → 双向收发」全链路（离线运行，无需凭据）。
- `docs/proxy-auto.md`：代理选优原理、配置、群晖 Docker 部署与排障文档。
- `/syncrestrictedmsg` command to sync restricted messages from channels that block forwarding, using Telegram's Takeout API for authorized bulk export.

### Changed
- Refactored core components with a shared `BaseComponent` base class for consistent logging and client access across syncer, forwarder, and restricted syncer.
- Restructured syncer internals for better separation of concerns and maintainability.

### Fixed
- 代理模式下的周期性重连：代理空闲超时（45~55s）早于 Telethon 心跳（60s），空载连接
  必然被掐断；现由 `core/keepalive.py` 缩短心跳周期解决。
- 代理复测误判：状态文件未持久化 `alive` 字段，导致 `main.py` 复测时永远认为「当前代理
  已失效」，每小时都会重启进程；现已随候选一同落盘，并按 `dc_ms` 比较优劣。
- Improved forum topic forwarding reliability by correctly handling the Telegram "General" topic (`id=1`) matching and synchronization behavior.

## [0.1.0] - 2026-03-17

### Added
- Initial Telegram forwarding bot with `Bot + UserBot` dual-client architecture.
- Private-link parsing for channels, groups, comments, and forum topics.
- `/sync` command for historical message backfill.
- `/monitor` command for real-time forwarding task creation.
- Multi-strategy forwarding engine with automatic degradation and recovery across direct send, user-assisted forwarding, and re-upload paths.
- Album-aware forwarding with grouped-send first and per-message fallback.
- Task management commands for listing and operating running tasks.
- Rate-limit settings view and operational controls.
- Failure marker output (`#fail2forward`) when all forwarding strategies fail.

### Changed
- Strengthened `/monitor` source-access validation to enforce UserBot readability before creating monitor tasks.
- Refined task information and operator-facing monitoring feedback.
- Improved hard-block filtering behavior for cross-platform restrictions.
- Improved reply-thread handling during forwarding and synchronization.
- Added tooling support for link-response diagnostics.
- Updated dependency/install workflow and service naming in deployment scripts.

### Fixed
- Resolved Telethon compatibility issue with `GetForumTopicsByIDRequest` imports (Telethon 1.42+).
- Restored strategy fallback behavior for message entity parsing failures.
- Fixed video-cover handling in degraded forwarding paths.
