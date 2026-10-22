# TG Forward Bot

[English](./README.en.md)

基于 Telethon 的 Telegram 消息转发工具，采用 **Bot + UserBot** 双客户端架构，支持私聊链接解析、历史消息同步、实时监控转发和多级策略自动降级。

## 功能特性

- **按钮菜单** — `/start` 打开图形菜单，向导式创建任务，无需记命令和参数
- **频道克隆** — 把任意频道/群组（含话题）从最早一条克隆到最新一条
- **起点可选** — 从头 / 从指定消息 ID / 从指定时间 / 继续上次进度
- **进度可见** — 进度条、百分比、速率、剩余时间，原地刷新不刷屏
- **断点续传** — 暂停、崩溃、重启后都从游标继续，不重复不遗漏
- **链接解析** — 私聊发送 `t.me/...` 链接即可触发，支持公开/私有频道、群组、评论区、话题
- **历史同步** — `/sync` 拉取历史消息，支持断点续传；受限消息自动通过 Takeout 内联转发
- **受限消息补发** — `/syncrestrictedmsg` 通过 Telegram Takeout 导出接口，仅补发被平台限制的消息
- **实时监控** — `/monitor` 或菜单创建，监听新消息自动转发；「📡 监控任务」页可查每个任务的源、目标与已转发条数，重复添加同一监控会被拦下
- **多级降级** — Bot 直发 → UserBot 辅助 → 下载重传 → 失败标记 `#fail2forward`
- **相册感知** — 优先整组发送，失败后逐条降级
- **硬封禁过滤** — 自动跳过全平台受限（`platform=all`）的内容；仅限部分平台的限制不受影响
- **双模式** — 支持 `copy`（默认）和 `forward` 两种转发模式
- **数据持久化** — 任务与断点存在 SQLite（`data/bot.db`），目录挂载即随镜像升级保留；菜单「💾 备份数据库」一键在线快照，启动自检会提示未持久化的路径（见 [docs/storage.md](docs/storage.md)）
- **代理自愈** — 网络无法直连 Telegram 时，自动从公共 MTProto 代理榜单选优（见 [docs/proxy-auto.md](docs/proxy-auto.md)）

克隆功能的完整说明见 [docs/clone.md](docs/clone.md)；监控任务的进度口径与防重复规则见 [docs/monitor.md](docs/monitor.md)；数据落盘、备份与迁移见 [docs/storage.md](docs/storage.md)。

## 架构

| 角色        | 职责                         |
| ----------- | ---------------------------- |
| **Bot**     | 命令交互、目标发送（写）     |
| **UserBot** | 受限来源读取、媒体下载（读） |

设计原则：**UserBot 读，Bot 写**，仅在必要时让 UserBot 参与写入。

## 环境要求

- Python 3.11+
- Telegram `api_id` / `api_hash`（[my.telegram.org](https://my.telegram.org)）
- Bot Token（[@BotFather](https://t.me/BotFather)）
- UserBot 账号（手机号登录）

## 快速开始

### 1. 安装与配置

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 编辑填入你的配置 (api_id, api_hash, bot_token 及 phone/phones)
```

### 2. UserBot 交互式登录与会话初始化

首次启动前，需通过交互脚本完成 UserBot 登录（支持多账号，按顺序依次登录）：

* **自适应脚本（推荐，Linux / VPS）**：
  ```bash
  bash scripts/login_userbot.sh -p +8613800138000
  ```
* **Python 直接运行**：
  ```bash
  python scripts/login_userbot.py -p +8613800138000
  ```
* **Docker 交互运行**：
  ```bash
  docker compose run --rm -it bot python scripts/login_userbot.py -p +8613800138000
  ```

> **说明**：
> 1. 终端提示输入短信验证码及 2FA 密码后，会话凭证将持久化在 `sessions/` 目录。
> 2. 若配置了多个 UserBot（在 `config.yaml` 的 `phones` 列表中），请针对每个号码分别运行一次登录。
> 3. `.session` 文件跨平台通用，亦可在本地电脑登录生成后直接复制到服务器 `sessions/` 目录。

### 3. 启动主服务

```bash
python main.py
```

## 命令

| 命令                          | 说明                                                  |
| ----------------------------- | ----------------------------------------------------- |
| `/sync <链接> [--forward]`    | 同步历史消息到当前会话；受限消息通过 Takeout 内联转发 |
| `/syncrestrictedmsg <链接>`   | 通过 Takeout 导出接口仅补发受限消息到当前会话         |
| `/monitor <链接> [--forward]` | 监控新消息并转发到当前会话（需 UserBot 已加入源）     |
| `/list`                       | 任务管理：暂停 / 恢复 / 删除 / 清空                   |
| `/settings`                   | 查看限流配置                                          |
| `/start` · `/help`            | 启动说明与帮助                                        |

私聊直接发送 `t.me/...` 链接即可触发解析与转发。

## 来源访问要求

| 来源类型          | `/sync` 对 UserBot 的要求 | `/monitor` 对 UserBot 的要求 | `/syncrestrictedmsg` 对 UserBot 的要求 |
| ----------------- | ------------------------- | ---------------------------- | -------------------------------------- |
| 公开频道 / 群组   | 通常无需加入              | **必须加入**                 | 必须加入（Takeout 要求）               |
| 私密频道 / 群组   | 必须已加入且可读          | **必须已加入且可读**         | 必须已加入且可读                       |
| 群组话题（Forum） | 需能读取该群              | **必须加入**                 | 需能读取该群                           |

- `/monitor` 创建任务前会校验 UserBot 是否已加入并可访问源，不满足会拒绝创建。
- `/syncrestrictedmsg` 使用 Telegram Takeout 导出接口，要求 UserBot 已加入源。先扫描全部消息识别受限内容，再通过 Takeout 会话批量补发。
- Bot 始终负责目标侧发送；来源侧读取能力取决于来源是否公开。

## 转发策略

运行时固定按以下顺序尝试，失败自动降级：

1. **Bot 直接处理** — 成本最低，Bot 可读源时优先命中
2. **UserBot 读取 + Bot 处理** — 源受限时由 UserBot 辅助读取
3. **UserBot 下载 + Bot 重新上传** — 源禁止转发或内容受保护时的兜底
4. **失败标记** — 全部失败后发送 `#fail2forward`

> **提示**：日志中的"策略1成功"表示第一层尝试成功，`copy` 模式下可能实际使用的是 `send_message` / `send_file`。评论链接 `?comment=` 的消息位于 discussion 群，主贴公开不代表评论区也公开。

## Docker 部署

官方镜像由 GitHub Actions 在每次代码更新后自动构建发布（支持 amd64 / arm64 双架构）：

```
ghcr.io/consoleaiden/tg-sync-autoforward-saverestricted-bot:latest
```

**方式一：docker compose（推荐）**

```bash
# 1. 准备配置文件
cp config.example.yaml config.yaml
#    编辑 config.yaml 填入 api_id / api_hash / bot_token 等

# 2. 首次交互式初始化 UserBot 登录
docker compose run --rm -it bot python scripts/login_userbot.py -p +8613800138000

# 3. 启动（自动拉取镜像）
docker compose up -d

# 以后更新：compose 里已内置 watchtower，会自动跟进 CI 新镜像；
# 想立刻生效时再手动执行
docker compose pull && docker compose up -d
```

本地开发想从源码构建时，取消 `docker-compose.yaml` 中 `build: .` 的注释后执行 `docker compose up -d --build`。

**方式二：群晖 Container Manager（套件中心安装）**

1. 打开 Container Manager → 注册表，搜索或直接下载 `ghcr.io/consoleaiden/tg-sync-autoforward-saverestricted-bot:latest`（群晖 UI 不便输入 GHCR 地址时，可用 SSH 执行 `docker pull`，再到「映像」里刷新可见）；
2. 用「项目」导入仓库里的 `docker-compose.yaml`，或手动创建容器并按下表添加挂载；
3. 映射路径（在共享文件夹如 `/docker/tg-sync-bot/` 下准备）：

| 宿主机          | 容器               | 说明                                                       |
| --------------- | ------------------ | ---------------------------------------------------------- |
| `./config.yaml` | `/app/config.yaml` | 配置文件（含凭据，不要提交/外泄）                          |
| `./data`        | `/app/data`        | **SQLite 数据库 `bot.db`（克隆进度/任务）、`bot.db-wal`、`backups/`、代理选优 `proxy.json`** |
| `./sessions`    | `/app/sessions`    | Bot / UserBot 登录会话                                     |

> `./data` 这一行是「升级镜像不丢进度」的关键：镜像重建只换容器，宿主机目录里的 `bot.db` 原样保留。
> 换成别的路径时必须同步加挂载，否则会落到容器可写层；启动日志与「⚙️ 限流设置」页都会显示数据库位置和
> 是否已持久化。细节见 [docs/storage.md](docs/storage.md)。

**镜像自动更新**（有新代码推送到 main 就跟最新版）：

- **推荐：compose 内置的 watchtower** —— `docker-compose.yaml` 里已带 `watchtower` 服务，
  它会每 10 分钟检查 GHCR，有新镜像就原地重建 bot。一次性执行 `docker compose up -d` 之后，
  **以后 push 代码就不用再 SSH 进群晖敲 pull**（详见 [docs/auto-update.md](docs/auto-update.md)）；
- **群晖 Container Manager**：「容器」→ 选中容器 →「自动更新」，勾选后会在检测到镜像有新版本时自动重建容器（挂载与配置保持不变）。

镜像里的版本指纹见镜像标签 `org.opencontainers.image.revision`（对应构建时的 git commit）。

## 数据持久化与备份

克隆进度、任务、消息映射全部在 SQLite 里（容器内 `/app/data/bot.db`），
只要 `./data` 挂到宿主机，`pull` 新镜像重建容器后任务与断点原样还在。

```bash
# 启动日志会自检落盘位置
数据库初始化完成 · 数据库 /app/data/bot.db（已存在，96.0 KB，已持久化）

# 手动备份（容器运行中也能做，SQLite 在线快照，不影响克隆）
docker compose exec bot python scripts/backup_db.py --keep 14
```

- 默认 WAL 模式：`docker stop`、升级重建、掉电都不会写坏库，已提交的进度自动回放；
- 菜单「💾 备份数据库」一键快照到 `data/backups/`，默认保留最近 7 份；
- 自定义位置用 `storage.db_path` 或 `TG_DB_PATH`，**记得同时加目录挂载**；
- 升级若报 `container name "/tg-sync-bot" is already in use`：旧容器不是本 compose 项目建的，
  `docker rm -f tg-sync-bot` 后重跑即可（数据在 `./data`，不受影响），步骤见
  [docs/storage.md](docs/storage.md#常见问题)；
- 完整说明（群晖映射、恢复步骤、迁移新机、常见报错）见 [docs/storage.md](docs/storage.md)。

## 无法直连 Telegram？MTProto 代理自动选优

网络无法直连 Telegram（如国内群晖 NAS）时，可开启内置的代理自动选优：每小时从
[telegram-mtproto-proxy-list](https://github.com/consoleaiden/telegram-mtproto-proxy-list)
拉取 `top40.json`，**在本机对每条代理做真实 FakeTLS 握手测速**，自动选用本机最快的可用代理，
代理失效或明显更优时自动切换。

```yaml
# config.yaml
proxy:
  mode: auto            # off（默认直连）/ auto / manual
  refresh_minutes: 60
```

```bash
# 排查用：看本机实测榜单
docker compose run --rm bot python -m core.proxy_manager --test
```

完整说明（原理、参数、群晖部署、排障）见 [docs/proxy-auto.md](./docs/proxy-auto.md)。

## 注意事项

- **受限消息由 UserBot 身份发出**：Takeout 获取的媒体引用绑定 UserBot，Bot 无法直接使用.
- `forward` 模式保留原始转发语义；`copy` 模式兼容性更好但更容易触发限流
- 私有来源必须确保 UserBot 已加入并可读，否则策略 2/3 均会失败
- 话题转发依赖 `target_topic_id`，目标侧权限不足会导致发送失败
- 频繁出现 `FloodWait` 时，建议调大 `rate_limit` 相关参数
- 相册优先整组发送，失败后降级为逐条转发

## 日志与排障

| Logger                             | 用途                       |
| ---------------------------------- | -------------------------- |
| `tg_forward_bot.handlers`          | 命令与私聊解析入口         |
| `tg_forward_bot.link_parser`       | 链接解析与 discussion 解析 |
| `tg_forward_bot.forwarder`         | 策略执行与降级             |
| `tg_forward_bot.syncer`            | 历史同步进度               |
| `tg_forward_bot.restricted_syncer` | 受限消息 Takeout 同步      |
| `tg_forward_bot.monitor`           | 实时监控事件               |

详细运维文档见 [`docs/operations.md`](docs/operations.md) 与 [`docs/architecture.md`](docs/architecture.md)。

### 常见启动失败

| 日志现象                                                            | 原因与处理                                                                                                                              |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `EOFError: EOF when reading a line` + `code_callback` 栈            | UserBot 没有可用会话，容器内无法输入验证码。执行 `docker compose run --rm -it bot python scripts/login_userbot.py -p <手机号>` 完成登录 |
| `UserBot [...] 尚未登录` / `登录会话已失效`                         | 同上；会话失效时先删除 `sessions/userbot_<手机号>.session` 再重新登录                                                                   |
| `proxy argument will be ignored because python-socks is not installed` | 仅提示信息，MTProxy 走连接类自身实现，代理仍然生效，可忽略                                                                              |
| `镜像拉取失败 ... Connection reset by peer`                         | 代理列表镜像被重置，会自动换下一个镜像；把可达镜像放 `DEFAULT_SOURCES` 首位即可减少等待                                                 |
| 容器日志时间比宿主机慢 8 小时                                       | 容器内为 UTC。`docker-compose.yaml` 已设 `TZ=Asia/Shanghai`，旧镜像需重新拉取                                                           |
