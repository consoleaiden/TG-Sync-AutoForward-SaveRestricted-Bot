# 代理覆盖审计：每个功能是否真的走了 MTProto 代理

> 结论：**所有访问 Telegram 的代码路径都经过代理**，不存在直连。
> 唯一的非代理出网是「拉取 top40.json 榜单镜像」（jsdelivr / GitHub，非 Telegram 域名）。
>
> 审计方式：在 socket 层劫持 `asyncio.open_connection`，记录每一次真实 TCP 建连，
> 再逐条触发功能链路，最后核对目标地址。脚本：`scripts/audit_proxy_egress.py`。

---

## 1. 逐功能核对表

| 功能 | 实际网络调用 | 执行客户端 | 走代理 | 验证方式 |
|------|-------------|-----------|:---:|---------|
| 链接解析（t.me 链接） | `get_entity` → `ResolveUsernameRequest`（MTProto） | Bot / UserBot | ✅ | 审计步骤「链接解析」实测解析 `telegram` 频道成功 |
| 历史同步 `/sync` | `GetHistoryRequest` + `download_media`（MTProto） | UserBot | ✅ | 审计步骤「历史同步」拉到 5 条；「媒体下载」实测 5.7MB |
| 受限消息补发 `/syncrestrictedmsg` | `client.takeout()` → `InvokeWithTakeoutRequest`，复用同一连接 | UserBot | ✅ | Telethon 的 `takeout()` 返回的是**当前 client 的代理对象**，不新建连接；见下方代码证据 |
| 实时监控 `/monitor` | 复用已登录 UserBot 的 Updates 通道，无独立连接 | UserBot | ✅ | 客户端由 `create_client` 统一构造（代理注入点唯一） |
| 多级降级（Bot→UserBot→下载重传） | Bot 发送 → UserBot 发送 → `download_media` 后 `send_file` | Bot / UserBot | ✅ | 每一级都在同一批已代理客户端上执行 |
| 相册感知（整组发送/逐条降级） | `SendMultiMediaRequest` / 逐条 `send_file` | Bot / UserBot | ✅ | 同上，无独立连接 |
| 硬封禁过滤（platform=all） | 纯本地字段判断，**不发网络请求** | — | —（不涉及） | 代码中为属性判断 |
| 双模式 copy / forward | copy=`send_message`/`upload`；forward=`ForwardMessagesRequest` | Bot / UserBot | ✅ | 同上 |
| 大文件并发加速（FastTelethon） | 4~6 条**额外**连接（同 DC 手工构造 sender / 跨 DC 导出授权） | Bot / UserBot | ✅ | 审计步骤「4 连接并发」与「跨 DC 多连接」均成功，8 次建连全指向代理 |
| UserBot 首次登录 | `client.start()` | 登录脚本 | ✅ | 登录脚本同样经 `create_client` |
| 代理榜单拉取 | HTTPS 到 jsdelivr / raw.githubusercontent / gh-proxy | — | ❌（非 Telegram） | 这是普通互联网访问，不需要 Telegram 代理；但容器需能访问这些 CDN |
| 代理测速 | TCP 直连候选代理本身 | — | —（预期直连代理） | 选优逻辑自身行为 |

## 2. 为什么 Takeout 不需要单独代理

Telethon 的实现是把请求包一层 `InvokeWithTakeoutRequest`，走**同一个** `MTProtoSender`：

```python
# telethon/client/account.py
def takeout(...) -> 'TelegramClient':
    """Returns a TelegramClient which calls methods behind a takeout session.
    It does so by creating a proxy object over the current client..."""
```

即 `async with client.takeout() as takeout:` 里的 `takeout` 是当前 client 的包装对象，
不会新建 TCP 连接，因此自动继承代理配置。

## 3. 为什么并发加速也走代理

`core/fast_telethon.py` 建立额外连接时显式透传了客户端的连接类与代理：

```python
await sender.connect(client._connection(
    dc.ip_address, dc.port, dc.id,
    loggers=client._log,
    proxy=client._proxy,        # ← 代理在这里透传
    local_addr=client._local_addr,
))
```

跨 DC 走 `client._borrow_exported_sender()`，Telethon 内部同样是
`self._connection(..., proxy=self._proxy)`（`telegrambaseclient.py:842`）。

## 4. 工程加固：客户端构造只有一个入口

原先 Bot、UserBot、登录脚本各自拼装 `connection`/`proxy`，新增代码容易漏传而在受限网络里
静默失败。现已收敛为唯一入口：

```python
# core/proxy_manager.py
def create_client(session, api_id, api_hash, *, proxy=None, **kwargs):
    kwargs.update(build_client_kwargs(proxy))   # 强制注入 connection + proxy
    client = TelegramClient(session, api_id, api_hash, **kwargs)
    if proxy is not None:
        verify_proxy_applied(client, "TelegramClient(%s)" % session)
    return client
```

`verify_proxy_applied()` 会同时校验 `client._connection` / `client._proxy`，以及**已建立连接的
真实传输类**是否与配置一致；不一致直接 CRITICAL 告警（能抓到"参数被中间层覆盖"这类隐蔽问题）。

并有回归测试 `test/test_proxy_coverage.py` 守住这条规则：静态扫描全仓库，除
`core/proxy_manager.py` 外**任何文件直接写 `TelegramClient(...)` 都会让测试失败**。

## 5. 在群晖上自证

```bash
# 基础审计（Bot + UserBot 的所有关键路径）
docker compose run --rm bot python scripts/audit_proxy_egress.py

# 额外验证 4 连接并发（大文件加速路径）
docker compose run --rm bot python scripts/audit_proxy_egress.py --fast

# 机器可读输出
docker compose run --rm bot python scripts/audit_proxy_egress.py --fast --json
```

期望输出（本机实测）：

```
=== TCP 建连目标统计 ===
  ✅ 150.241.103.119:443             8 次   代理
✅ PASS：8 次建连全部指向代理，未发现直连 Telegram 的行为
```

判定规则：出现任何落在 Telegram 官方网段（`149.154.160.0/20`、`91.108.4.0/22` 等）的
连接即判 FAIL，并列出该目标。

## 6. 已知的"看起来像问题但不是问题"的情况

| 现象 | 说明 |
|------|------|
| `Takeout 跳过（Telegram 要求等待 86400s）` | Telegram 对新账号/频繁导出有 24h 冷却，属服务端限流，与代理无关 |
| `BotMethodInvalidError: GetHistoryRequest` | Bot 身份无权拉历史，这类功能本就由 UserBot 执行 |
| 审计统计里代理连接数只有 3~4 次 | 每个客户端只维持 1 条长连接；并发加速/跨 DC 才会额外建连 |
| 榜单拉取走的是公网 HTTP | 拉 `top40.json` 不需要 Telegram 代理，只需容器能出网访问 CDN |
