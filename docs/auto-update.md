# 自动更新：push 代码 → CI 构建 → 群晖自动换镜像

目标：在 GitHub 上推了代码后，**不需要再 SSH 进群晖敲 `docker compose pull`**，
NAS 上的容器自己换成 CI 刚构建好的镜像。

链路：

```
git push main ──► GitHub Actions 构建 amd64/arm64 ──► 推送 GHCR :latest + :sha-xxxxxxx
                                                              │
                         群晖上的 watchtower 每 10 分钟查一次 ─┘
                                    │
                          发现 digest 变了 → pull → 优雅停止旧容器 → 用同一份
                          配置（卷/环境变量/网络）重建 → bot 自动断点续传
```

## 方案对比

| 方案                          | 是否要 SSH | 可靠性 | 说明                                             |
| ----------------------------- | ---------- | ------ | ------------------------------------------------ |
| **Watchtower（本仓库已内置）** | 只有首次   | 高     | 轮询 registry，按标签只升指定的容器，最省心        |
| 群晖 Container Manager「自动更新」 | 否     | 中     | 依赖 DSM 版本，触发时机不透明，偶发不生效          |
| GitHub Actions SSH 到群晖      | 否         | 中     | 需要把 NAS 的 SSH 暴露给 GitHub，凭据泄露面更大    |

## 方式一：Watchtower（推荐，compose 已内置）

`docker-compose.yaml` 里已经加好了 `watchtower` 服务和 bot 上的
`com.centurylinklabs.watchtower.enable=true` 标签，**只需要做一次**：

```bash
cd /volume2/docker/TG-Sync-Bot
docker compose up -d          # 拉起 watchtower（bot 也会顺手更新到最新）
docker compose logs -f watchtower
```

看到类似日志就说明在正常工作：

```
Found update (new image: sha256:...) for container tg-sync-bot
Stopping /tg-sync-bot (SIGTERM)...
Creating /tg-sync-bot
Session done: 1 updated, 0 failed
```

之后每次 `git push` → Actions 构建完成 → 最多 10 分钟，NAS 上就是新镜像。

**验证真的换了镜像**（可选）：

```bash
docker inspect tg-sync-bot --format '{{.Image}}'      # 与 GitHub Actions 产物 digest 对应
docker images | grep tg-sync
docker compose logs --tail=30 bot                     # 看到「数据库初始化完成 · ...（已持久化）」
```

### 关键参数

| 环境变量                     | 默认  | 作用                                          |
| ---------------------------- | ----- | --------------------------------------------- |
| `WATCHTOWER_POLL_INTERVAL`   | `600` | 轮询间隔（秒）。想更快可写进 `.env`：`WATCHTOWER_POLL_INTERVAL=300` |
| `WATCHTOWER_CLEANUP`         | true  | 升级后删旧镜像，避免 NAS 磁盘被历史层占满      |
| `WATCHTOWER_LABEL_ENABLE`    | true  | 只升级带 `watchtower.enable=true` 标签的容器   |

**私有 GHCR 包**（仓库是 public 时不用管）：在 NAS 上 `docker login ghcr.io -u <用户名>`
后取消 compose 里 `WATCHTOWER_REGISTRY_AUTH` 与
`/root/.docker/config.json:/config.json:ro` 两行注释。

### 它不会做的事

- **不会改 compose 里的配置**：新增卷、新增环境变量、改端口这类改动，仍需你手动
  `docker compose up -d` 一次（Watchtower 只沿用容器现有配置重建）。
- **不会升级自己**：watchtower 镜像固定了版本号，就是为了避免它把自己升级坏。
- **不会升级别的容器**：开了 `LABEL_ENABLE`，只有 bot 带标签。

### 回滚到上一个版本

镜像还留着 `sha-xxxxxxx` 标签（对应 commit）：

```yaml
# docker-compose.yaml，临时改一行
    image: ghcr.io/consoleaiden/tg-sync-autoforward-saverestricted-bot:sha-1a2b3c4
```

```bash
docker compose up -d bot      # watchtower 会把它改回 latest，记得随后改回来
```

数据库结构只做「加列」迁移（`ALTER TABLE ADD COLUMN`），回退旧版镜像不会因为多出来的列报错；
但旧版写入的数据不会回退，必要时用 `data/backups/` 里的快照恢复（见 [storage.md](./storage.md)）。

## 方式二：群晖 Container Manager「自动更新」

不想多跑一个容器时可用：Container Manager →「容器」→ 选中 `tg-sync-bot` → 勾选「自动更新」。

局限（务必知道）：

- 不同 DSM 版本行为不一致，有的只在打开套件时检查一次，不是后台定时；
- 对 compose「项目」创建的容器支持程度因版本而异；
- 不生效时没有明显提示，容易出现「以为自动更新了，其实还在跑旧代码」。

## 方式三：GitHub Actions 直接 SSH 到群晖（不推荐）

原理是 CI 构建完用 `appleboy/ssh-action` 执行 `docker compose pull && up -d`。
需要把 NAS 的 SSH 端口暴露到公网（或内网穿透）并存放私钥，攻击面明显变大；
Watchtower 已经把这件事做得更简单，除非你的 NAS 与 registry 网络隔离，否则不建议。

## 常见问题

**push 了代码，NAS 一直没更新**

1. 先在 GitHub 仓库「Actions」确认 `Build Docker Image` 成功（失败则没有新镜像）；
2. `docker compose logs watchtower | tail -50` 看是否在轮询、是否报 401/网络错误；
3. 确认 bot 容器带标签：`docker inspect tg-sync-bot --format '{{json .Config.Labels}}'`；
4. 手动触发一次：`docker compose run --rm watchtower --run-once --debug`。

**更新后还是旧行为**

多半是 CI 还没跑完（构建 + 双架构推送约 3–6 分钟），或者本地镜像被缓存。
`docker compose pull bot && docker compose up -d bot` 可强制对齐。

**更新时正在克隆，会断吗？**

会暂停一下：容器被停止 → 新镜像启动，进度不受影响。WAL 保证已提交的游标与计数完整落盘，
下次打开自动回放；启动时 `clone.auto_resume: true` 会从 `last_synced_msg_id` 继续，
不重复不遗漏。详见 [clone.md](./clone.md) 与 [storage.md](./storage.md)。
