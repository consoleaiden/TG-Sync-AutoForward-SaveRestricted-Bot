# 数据持久化、备份与迁移

克隆进度、任务列表、消息映射都保存在一个 SQLite 文件里。容器**升级镜像会重建容器**，
只有挂载到宿主机的目录才会保留 —— 本文说明数据在哪、怎么确保不丢、怎么备份和迁移。

## 数据都在哪里

| 路径                     | 内容                                              | 丢了会怎样                       |
| ------------------------ | ------------------------------------------------- | -------------------------------- |
| `/app/data/bot.db`       | 任务表 `tasks`（含克隆游标 `last_synced_msg_id`）、消息映射表 `message_map` | 所有任务与断点消失，需从头克隆 |
| `/app/data/bot.db-wal`   | WAL 日志（未合并进主库的最新提交）                | 同上，且单独拷贝 `bot.db` 可能少最后几条 |
| `/app/data/bot.db-shm`   | WAL 共享内存索引                                  | 可重建，不影响数据               |
| `/app/data/proxy.json`   | 代理自动选优结果                                  | 下次启动重新测速，无损失         |
| `/app/data/backups/`     | 「💾 备份数据库」生成的快照                       | 只是备份，无影响                 |
| `/app/sessions/*.session`| Bot / UserBot 登录会话                            | 需要重新登录（可能要验证码）     |

`config.yaml` 里的凭据不属于数据库，但同样建议挂载并单独备份。

## 宿主机挂载（默认方案）

`docker-compose.yaml` 已经是最小可用配置：

```yaml
services:
  bot:
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./data:/app/data          # bot.db + WAL + 备份 + proxy.json
      - ./sessions:/app/sessions  # 登录会话
```

只要 `./data` 指向宿主机磁盘（群晖共享文件夹、VPS 本地目录），
`docker compose pull && docker compose up -d`、群晖「自动更新」重建容器都不会动这些文件。

### 群晖 Container Manager

1. 在共享文件夹（如 `docker/tg-sync-bot/`）下建 `data`、`sessions` 两个目录；
2. 项目方式：直接用仓库里的 `docker-compose.yaml`，`./data` 相对项目目录解析；
3. 手动创建容器时，「存储空间」里必须加两条映射：
   `/docker/tg-sync-bot/data` → `/app/data`，`/docker/tg-sync-bot/sessions` → `/app/sessions`；
4. 升级：Container Manager 里勾选「自动更新」，或在 SSH 里 `docker compose pull && docker compose up -d`；
5. 验证：升级后打开 bot 菜单「⚙️ 限流设置」，底部「💾 数据存储」会显示数据库绝对路径与
   `✅ 已挂载到宿主机，升级镜像不会丢`。

> **不要直接把单个 `bot.db` 文件挂载进容器。** 文件不存在时 Docker 会创建成目录，
> SQLite 打不开就会报 `unable to open database file`。要单独放就挂载**目录**（见下）。

## 自定义数据库位置

优先级：环境变量 `TG_DB_PATH` > `config.yaml` 的 `storage.db_path` > 默认 `data/bot.db`。

```yaml
# config.yaml
storage:
  db_path: data/bot.db   # 容器内相对路径按 WORKDIR /app 解析
  wal: true              # 放到 SMB/NFS 网络盘时改 false
  backup_dir: ""         # 留空 = 数据库同级的 backups/
  backup_keep: 7
```

换路径后**必须同步加一条目录挂载**，否则就落到容器可写层了：

```yaml
    environment:
      TG_DB_PATH: /app/db/bot.db
    volumes:
      - ./db:/app/db
```

## 启动自检

每次启动会打印数据库位置、大小与持久化状态：

```
数据库初始化完成 · 数据库 /app/data/bot.db（已存在，96.0 KB，已持久化）
沿用已有数据库，历史任务与克隆进度保持不变
```

位置不对时会给一条 `WARNING`，明确写出当前路径、已挂载目录和两种修复方式：

```
检测到数据库不在宿主机挂载目录内，重建容器/升级镜像会丢失全部克隆进度！
  当前路径：/app/tasks/bot.db
  已挂载目录：/app/data, /app/sessions
  ...
```

容器内被认为「已持久化」的目录默认是 `/app/data` 与 `/app/sessions`（与 compose 一致），
可用 `storage.persist_dirs` 或 `TG_PERSIST_DIRS=/app/data,/app/backup` 覆盖。

## WAL：为什么多了两个文件

数据库默认开启 WAL（`storage.wal: true`）：

- 写事务不阻塞读，克隆边写进度边查任务不会互相卡住；
- 进程被 `docker stop` / 升级重建 / 掉电打断时，下次打开会自动回放 `bot.db-wal`，
  已提交的进度不会丢，也不会把库写成半截；
- `busy_timeout=5000` 让备份脚本与 bot 同时访问时自动等锁，而不是报 `database is locked`；
- 正常退出时会自动 `wal_checkpoint(TRUNCATE)` 把 WAL 合并回 `bot.db`。

注意：

- 手工拷贝数据库时要**连 `bot.db-wal`、`bot.db-shm` 一起复制**，或者干脆用下面的备份功能；
- 把 `data` 放在 SMB/NFS 网络盘上时，WAL 可能不可靠，请设 `storage.wal: false`。

## 备份

**方式一：菜单按钮**（推荐，随时可点）

`/start` → 「💾 备份数据库」，在 `data/backups/` 生成 `bot-YYYYmmdd-HHMMSS.db`，
回一条消息告诉你文件名、大小、当前份数与恢复步骤，超出 `backup_keep`（默认 7 份）自动删最旧。

**方式二：命令行 / 定时任务**

```bash
# 容器运行中也能备份（SQLite 在线快照，不影响克隆）
docker compose exec bot python scripts/backup_db.py
docker compose exec bot python scripts/backup_db.py --keep 14
```

放到群晖「任务计划」里每天跑一次（用户 root）：

```sh
cd /volume1/docker/tg-sync-bot && docker compose exec -T bot python scripts/backup_db.py --keep 14
```

`.*db` 快照也可以直接拷到别的盘做异地备份 —— 它是一个完整、可直接打开的独立数据库。

## 恢复

1. `docker compose stop`（群晖：容器 → 停止）；
2. 删掉 `data/bot.db`、`data/bot.db-wal`、`data/bot.db-shm`（WAL 残留会覆盖旧数据）；
3. 把备份复制成 `data/bot.db`：
   ```bash
   cp data/backups/bot-20261022-170000.db data/bot.db
   ```
4. `docker compose up -d`，任务列表与断点回到备份时刻。

## 迁移到新机器 / 新 NAS

1. 停容器；
2. 整目录拷走 `data/`（含 `backups/`）、`sessions/`、`config.yaml`；
3. 新机器按同样路径放好，`docker compose up -d`；
4. 看启动日志是否出现「沿用已有数据库」，菜单「⚙️ 限流设置」确认状态为 `✅ 已挂载到宿主机`。

会话文件一起拷走就不用重新登录；只拷 `data/` 则登录态需重建（`scripts/login_userbot.py`）。

## 常见问题

**升级镜像后任务列表空了**

先看启动日志里数据库路径：如果变成了容器内的临时路径，说明挂载丢了（群晖重建容器时最容易发生），
把 `data` 目录重新映射回宿主机、把旧 `bot.db` 放回去重启即可。

**`unable to open database file` / `bot.db` 变成了目录**

docker 把不存在的宿主机文件当目录创建了。删掉那个目录，改挂载 `./data:/app/data` 整个目录。

**权限报错（群晖常见）**

宿主目录属主与容器内的 uid 不一致。`chown -R 1000:1000 data sessions`，或把目录权限设为
`drwxr-xr-x` 并让容器以 root 运行（默认即是）。
