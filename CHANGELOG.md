# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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

### Changed
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
