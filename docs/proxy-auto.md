# MTProto 代理自动选优（无直连 Telegram 网络环境）

适用于部署在无法直连 Telegram 的网络（如国内群晖 NAS）中，用公共 MTProto 代理中转。
数据来自 [telegram-mtproto-proxy-list](https://github.com/consoleaiden/telegram-mtproto-proxy-list)
每小时更新的 `top40.json`，**在本机实测速度**后选最优代理给 Bot / UserBot 使用。

---

## 1. 它解决什么问题

| 问题 | 本项目做法 |
|------|-----------|
| 服务器直连 Telegram 超时 | 走 MTProto（MTProxy）代理中转 |
| 公共代理寿命短、随时失效 | 每小时拉榜单 + 本机复测，失效立即换 |
| 榜单耗时来自 GitHub runner，与本机差异巨大 | 在本机对每条代理做**真实 FakeTLS 握手**测速后排序 |
| GitHub / raw 域名可能不可达 | 内置多镜像（jsdelivr 多节点、raw、gh-proxy）依次回退 |
| Telegram 官方客户端能用 ee 型代理，但 Telethon 不行 | 自实现 `ConnectionTcpMTProxyFakeTLS`（见下） |

### 关键技术点（踩过的坑，已解决）

1. **Telethon 1.45 不支持 FakeTLS**：内置 `ConnectionTcpMTProxy*` 只处理裸 16 字节和
   `0xDD` 型 secret，`normalize_secret()` 会直接剥掉 `ee` 前缀、丢掉伪装域名，
   不会发 TLS ClientHello。而 top40 榜单几乎全是 `0xEE`（FakeTLS）代理，
   直接传 `proxy=('mtproto', ...)` 是连不上的。
2. **mtg（绝大多数公共代理的实现）只接受 padded intermediate**：混淆头帧内偏移
   56..60 的连接类型必须是 `dd dd dd dd`。用 abridged（`efefefef`）或
   intermediate（`eeeeeeee`）会被判非法握手，**回落到伪装网站并返回
   `HTTP/1.1 400 Bad Request`**。本项目固定使用 padded intermediate，与官方客户端一致。
3. **握手后第一条 ApplicationData 是伪装证书**（1–4KB 随机字节），不是 MTProto 数据，
   必须丢弃，否则解密流错位。
4. **握手通过 ≠ 能传数据**：实测有代理可以完成 FakeTLS 握手、HMAC 校验也通过，但收到
   真实 MTProto 流量后立刻断连（`Proxy closed the connection after sending initial
   payload`）。只按握手延迟排序会选中这类"假活"代理。因此选优流程增加了 DC 往返校验
   （见下节）。
5. **代理空闲超时 ≈ 45~55 秒，而 Telethon 心跳硬编码 60 秒**（`client/updates.py`），
   两者错位会让空载连接**必然**被代理掐断，日志反复出现
   `Server closed the connection: 0 bytes read on a total of 4 expected bytes`。
   本项目通过 `core/keepalive.py` 把心跳周期改为 20 秒（可配）解决；实测连续 150 秒
   空载零断连。

---

## 2. 工作流程

```
每小时（refresh_minutes）
   │
   ├─ 拉取 top40.json ── 多镜像回退 ──> 40 条候选（含 secret/伪装域名）
   │
   ├─ 本机并发 FakeTLS 握手测速（默认 40 并发、每条 2 次取中位数）
   │     • 校验服务端整包 HMAC：区分「密钥有效」与「假握手回落」
   │     • 记录 connect_ms / tls_ms / latency_ms
   │
   ├─ 初级排序：校验通过 → 能握手 → 失效；同档按延迟升序
   │
   ├─ DC 往返校验（默认前 8 名并发）
   │     • 经代理向 DC2 发一次明文 req_pq_multi
   │     • 只有拿到 nonce 匹配的 resPQ 才算「真能传数据」
   │     • 通过的按往返耗时排到最前（dc_ms），未通过的降为回退候选
   │
   ├─ 原子写入 data/proxy.json（含 current + 前 5 名 candidates）
   │
   └─ 需要切换时 → 进程退出 → Docker restart 策略拉起 → 启动时加载新代理
```

**为什么要有 DC 往返校验**：握手只是 TLS 层的事，代理完全可能在握手后按 IP/地区/
流量特征丢弃连接。实测同一轮里 8 个「握手全过、HMAC 全过」的代理，只有 3~4 个能
完成 req_pq 往返。没有这一步，Bot 会选中"看起来最快但传不了数据"的代理。

**切换判定**（避免每小时无意义重启）：

- 当前代理在新一轮测速中已失效 → 立刻切；
- 当前代理仍可用，但新最优代理耗时 < 当前 × `switch_improve_ratio`（默认 0.7，
  即快 30% 以上）→ 切换；比较时优先用 `dc_ms`（真实往返），缺失时退回 `latency_ms`；
- 否则保持当前代理不动（日志会说明原因）。

想严格「每小时都切到榜单第一」，把 `switch_improve_ratio` 设为 `1.0`。

启动时若还没有状态文件，会先同步执行一轮选优（最多重试 3 次）；仍失败则退出进程，
由 Docker 重启策略重试，不会在无代理状态下空转。

---

## 3. 配置

`config.yaml`：

```yaml
proxy:
  mode: auto              # off=直连 | auto=自动选优（推荐） | manual=固定代理
  refresh_minutes: 60     # 复测周期（分钟，最小 5）
  state_file: data/proxy.json
  switch_improve_ratio: 0.7
  sources: []             # 留空用内置镜像；可自定义，如 ["https://cdn.jsdelivr.net/gh/....../top40.json"]
  probe:
    concurrency: 40       # 并发探测数
    attempts: 2           # 每台探测次数
    timeout: 6            # 握手超时（秒）
    connect_timeout: 5    # TCP 建连超时（秒）
    dc_validate_top: 8    # 对握手前 N 名做真实 req_pq 往返校验
    dc_validate_timeout: 8  # 单次往返超时（秒）
  keepalive_seconds: 20   # 心跳间隔；0=关闭补丁（恢复 Telethon 默认 60 秒）
  manual:                 # mode: manual 时生效，从 t.me/proxy 链接复制
    server: "1.2.3.4"
    port: 443
    secret: "ee...."
```

`mode: off` 时行为与改造前完全一致（直连），不会启动任何代理任务。

`keepalive_seconds` 说明：公共代理普遍在 45~55 秒空闲后断开，而 Telethon 默认 60 秒才
发心跳，必然出现周期性重连。默认 20 秒可消除该现象；若你的代理不限制空闲连接，设 0
可减少心跳流量。

---

## 4. 群晖 Docker 部署

### 4.1 准备文件

把项目目录上传到群晖（如 `/volume1/docker/tg-forward-bot`），确保有：

```
config.yaml        # 从 config.example.yaml 复制后填好 api_id/api_hash/bot_token/phones
data/              # 自动创建
sessions/          # UserBot 登录后生成
```

`proxy.state_file` 里的 `data/proxy.json` 会在首次启动时自动生成，无需手工准备。

### 4.2 首次 UserBot 登录（需要短信验证码）

在群晖 SSH 中执行（也可在本地电脑登录后把 `sessions/` 拷到群晖）：

```bash
cd /volume1/docker/tg-forward-bot
sudo docker compose run --rm bot python scripts/login_userbot.py -p +8613800000000
# 输入收到的验证码；若开启了两步验证，再输入 2FA 密码
```

登录成功后 `sessions/` 下会生成 session 文件。

### 4.3 启动

```bash
sudo docker compose up -d --build
sudo docker compose logs -f --tail=100
```

启动日志应依次出现：

```
沿用已选代理：x.x.x.x:PORT（更新于 ...）      ← 或「开始执行代理选优」
已启用 MTProto 代理：x.x.x.x:PORT（连接类 ConnectionTcpMTProxyFakeTLS）
Bot 客户端已启动
代理自动复测任务已启动（每 60 分钟）
所有 UserBot 客户端启动完成，共计 1 个
```

### 4.4 群晖 Container Manager 图形化注意事项

- 项目需用「项目（docker-compose）」方式创建，`docker-compose.yaml` 已包含
  `restart: unless-stopped`——**代理切换依赖该重启策略**，请不要改成 `no`。
- 不需要给容器加 `network_mode: host`，也不需要额外端口映射。
- 容器内要能访问 `cdn.jsdelivr.net` / `raw.githubusercontent.com`（拉榜单用）。
  若群晖 DNS 有污染，可在「项目 → 设置」里给容器加 DNS（如 `223.5.5.5`）。

---

## 5. 手动工具

```bash
# 只测速、打印榜单（不写状态文件），排查「为什么选了这个代理」
docker compose run --rm bot python -m core.proxy_manager --test

# 跳过 DC 往返校验，只看握手速度（更快，但结果不代表真能传数据）
docker compose run --rm bot python -m core.proxy_manager --test --no-dc

# 立即执行一轮选优并写状态（不重启进程，下次启动生效）
docker compose run --rm bot python -m core.proxy_manager --once

# 指定自定义镜像（例如自建反代）
docker compose run --rm bot python -m core.proxy_manager --test \
  --sources https://your-mirror/top40.json
```

本地自检（不联网、不需要凭据）：

```bash
docker compose run --rm bot python -m unittest test.test_faketls_local -v
```

该用例自带一个 Mock FakeTLS 代理服务端（实现服务端摘要算法与 MTProxy 混淆密钥派生），
验证「握手 → TLS 记录封装 → 混淆头 tag/dc_id → 双向收发」全链路。

---

## 6. 排障

| 现象 | 原因与处理 |
|------|-----------|
| 日志 `所有 top40.json 镜像均拉取失败` | 容器出网/DNS 问题。用 `python -m core.proxy_manager --test` 看具体报错；或配 `proxy.sources` 指向可达镜像 |
| 日志 `本轮测速全部代理失效，保持现有代理配置不变` | 榜单当轮全挂或被网络阻断；旧配置不会被覆盖，下一小时自动重试 |
| 反复重启 | 检查 `switch_improve_ratio`；每轮都换说明当前代理频繁失效，可把 `refresh_minutes` 调小（如 20）以便更快切换 |
| 启动卡在 `Connecting to ...TcpMTProxyFakeTLS...` | 代理已被墙或端口被封；重启容器会重新选优，或临时改 `mode: manual` 指定固定代理 |
| 日志出现 `FakeTLS 握手失败: 服务端摘要校验未通过` | 该代理 secret 已失效（服务端回落假握手）；选优阶段已排除这类节点，手动指定代理时才会遇到 |
| 日志反复 `Server closed the connection: 0 bytes read on a total of 4 expected bytes` 并自动重连 | 代理空闲超时早于心跳。确认 `proxy.keepalive_seconds` 未被设为 0（默认 20 秒即可消除） |
| 连接正常但请求总是断、选中的代理"看着最快却传不了数据" | 该国该轮没有任何代理通过 DC 往返校验（日志 `DC 往返校验无一通过`）。可把 `probe.dc_validate_top` 调大（如 15）扩大验证面 |
| 想完全关闭代理 | `proxy.mode: off`，重启容器 |

---

## 7. 安全与合规提示

- 公共代理为互联网公开收集，**不保证可用性与安全性**，请勿通过它传输敏感信息；
  代理运营方理论上可以看到流量元数据（MTProto 内容本身仍是加密的）。
- 代理只用于突破网络不可达，账号凭据（api_hash / bot_token / session）始终只在
  你自己的服务器与 Telegram 之间使用。
- `data/proxy.json`、`sessions/`、`config.yaml` 均已在 `.gitignore` 中，不要提交到仓库。

---

## 8. 相关文件

| 文件 | 作用 |
|------|------|
| `core/mtproxy_probe.py` | FakeTLS 握手测速引擎（纯标准库，源自上游 proxy-list 项目） |
| `core/faketls_mtproxy.py` | Telethon `ConnectionTcpMTProxyFakeTLS`（FakeTLS + TLS 记录封装 + padded intermediate） |
| `core/proxy_manager.py` | 拉榜单、测速、DC 往返校验、选优、状态文件、Telethon 集成、CLI |
| `core/keepalive.py` | 连接保活补丁（心跳 20 秒，避免代理空闲断连） |
| `scripts/audit_proxy_egress.py` | 出网审计：证明所有 Telegram 访问都经代理（`docs/proxy-audit.md`） |
| `docs/proxy-audit.md` | 逐功能代理覆盖核对表与自证方法 |
| `main.py` | 启动解析代理、每小时复测、断线重选、切换后重启 |
| `core/userbot_manager.py` | UserBot 客户端透传代理参数 |
| `test/test_faketls_local.py` | Mock FakeTLS 服务端 + 全链路集成测试 |
