# TG Forward Bot

[中文文档](./README.md)

A Telethon-based Telegram message forwarding tool with a **Bot + UserBot** dual-client architecture. Supports link parsing, historical message sync, real-time monitoring, and multi-strategy graceful degradation.

## Features

- **Button Menu** — `/start` opens a guided, button-driven menu; no commands or arguments to memorise
- **Short Commands** — `/kl` clone & keep following, `/kl 100-1100` clone an ID range, `/jt` follow only, `/kllb` / `/jtlb` task lists, `/kljx` resume-and-catch-up, `/chapd` identify a source channel
- **Channel Cloning** — Clone any channel/group (including forum topics) from the earliest message to the latest
- **Selectable Start Point** — From the beginning, from a message ID, from a message ID **range**, from a date/time, or resume the previous run
- **Live Progress** — Progress bar, percentage, speed and ETA, refreshed in place on a single message
- **Integrity Report** — Every finished clone reports `source N / mapped M / coverage X% / gap K`, so an archive run tells you at a glance whether it got everything
- **Retry Failed Items** — `/klfail` lists what failed and why; `/retryfail <id|all>` re-sends them without touching the resume cursor
- **Adjustable Speed** — `/sd` command tunes forwarding pace for Bot and UserBot separately (presets or custom ranges), effective immediately without restart
- **Resumable** — Pause, crash or restart and it continues from the saved cursor with no duplicates or gaps
- **Idempotent** — A `UNIQUE(task_id, source_msg_id)` index on `message_map` plus a pre-send lookup means re-runs never duplicate messages in the target
- **Stall Recovery** — Per-strategy timeout (1200s) stops a dead connection from freezing the task; an independent watchdog alerts you after 10 minutes without progress
- **File Reference Refresh** — On `FILE_REFERENCE_EXPIRED` the message is re-fetched for a fresh reference and retried (up to 2 times), so long runs stop losing media in bulk
- **Concurrency Gate** — Only one clone runs at a time by default (`clone.max_concurrent`); the rest queue, because parallel runs multiply FloodWait
- **Records Never Disappear** — Finished clones (`completed`) stay in the clone task list, so you can `/kljx` to catch up or re-enable following at any time; deletion is a soft delete
- **Auto Follow-After-Finish** — When a clone finishes it keeps watching for new messages (`clone.follow_after_finish`) and stays visible in the monitor list; toggle it per task with 🛰 / ⏹ in the task detail
- **Link Parsing** — Send a `t.me/...` link in private chat to trigger forwarding; supports public/private channels, groups, comments, and forum topics
- **Historical Sync** — `/sync` pulls past messages with resumable progress tracking; restricted messages are automatically forwarded via Takeout inline
- **Restricted Message Sync** — `/syncrestrictedmsg` re-exports only platform-restricted messages using Telegram's Takeout API
- **Real-Time Monitoring** — `/jt` or the menu watches for new messages and forwards them automatically; the monitor list shows each task's source, target and forwarded count, and duplicate monitors are rejected
- **Source Identification** — Run `/chapd`, then forward any message from the channel into the chat; the bot returns the channel title, @username, ID, a usable link and ready-to-copy commands. `/chaid` returns the **message ID and message link** instead, pairing with `/kljx <link> <start|range>`
- **Graceful Degradation** — Bot direct → UserBot direct forward (server-side, no download) → UserBot read + Bot write → download & re-upload → failure marker `#fail2forward`
- **Album-Aware** — Sends media groups as a batch first, falls back to per-message on failure
- **Hard-Block Filtering** — Automatically skips globally restricted content (`platform=all`); platform-specific restrictions are still forwarded
- **Dual Mode** — Supports both `copy` (default) and `forward` transfer modes

See [docs/clone.md](docs/clone.md) for the cloning guide (start points, progress fields, resume semantics, FAQ).

## Architecture

| Role        | Responsibility                                  |
| ----------- | ----------------------------------------------- |
| **Bot**     | Command handling, target-side delivery (write)  |
| **UserBot** | Restricted-source access, media download (read) |

Design principle: **UserBot reads, Bot writes** — except when Bot cannot read the source at
all (a private channel Bot was never added to); then the UserBot forwards directly, which is a
server-side move with no download (see [Forwarding Strategy](#forwarding-strategy)).

## Prerequisites

- Python 3.11+
- Telegram `api_id` / `api_hash` ([my.telegram.org](https://my.telegram.org))
- Bot Token ([@BotFather](https://t.me/BotFather))
- A UserBot account (phone number login)

## Getting Started

### 1. Installation & Configuration

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # Fill in api_id, api_hash, bot_token, and phone/phones
```

### 2. UserBot Login & Session Initialization

Before starting the bot, initialize UserBot sessions via the interactive login tool (supports multiple accounts):

* **Using the auto-adaptive script (Recommended on Linux / VPS)**:
  ```bash
  bash scripts/login_userbot.sh -p +8613800138000
  ```
* **Using Python directly**:
  ```bash
  python scripts/login_userbot.py -p +8613800138000
  ```
* **Using Docker interactive run**:
  ```bash
  docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000
  ```

> **Notes**:
> 1. Follow the terminal prompt to enter the SMS verification code and 2FA password. Session files are persisted in `sessions/`.
> 2. If configuring multiple UserBots (under `phones` in `config.yaml`), run the login command once for each phone number.
> 3. `.session` files are cross-platform SQLite databases; you can also log in on a local machine and copy the generated files to the server's `sessions/` directory.

### 3. Start the Bot

```bash
python main.py
```

## Commands

**Primary commands (recommended)**

| Command                        | Description                                                                       |
| ------------------------------ | --------------------------------------------------------------------------------- |
| `/kl <link>`                   | Clone all messages to this chat, then keep following new messages automatically   |
| `/kl <start>-<end> <link>`     | Clone only a message ID range (inclusive), e.g. `/kl 100-1100 https://t.me/xxx`   |
| `/kljx <link>`                 | Continue cloning from the saved cursor up to the newest; nothing is duplicated    |
| `/kljx <link> 120`             | Continue cloning starting at message 120 (get the ID via `/chaid`)                |
| `/kljx <link> 120-1000`        | Clone only msg 120–1000 (the range may also precede the link)                     |
| `/jt <link>`                   | Follow new messages only (no history backfill)                                    |
| `/kllb`                        | Clone task list (includes completed tasks — records are kept forever)             |
| `/jtlb`                        | Monitor task list (includes follow-ups created from clone tasks)                  |
| `/klfail`                      | Show tasks with failures; `/klfail 12` lists task #12's failed items and reasons  |
| `/retryfail <id\|all>`         | Re-send failed items (keeps the resume cursor untouched)                          |
| `/sd`                          | View clone speed thresholds; `/sd bot fast\|max\|safe`, `/sd ub fast\|safe` presets |
| `/sd bot 0.3-0.8`              | Custom per-message delay; `/sd ub pause 30 45-90` custom UserBot batch pause        |
| `/chapd`                       | Prompts you to forward a channel message here to identify title / @username / ID  |
| `/chaid`                       | Prompts you to forward a channel message here to get its **ID** and **link**      |
| `/help`                        | Show every command with its purpose                                               |
| `/start` · `/menu`             | Button menu: wizard, progress, pause/resume, toggle follow                        |
| `/settings`                    | Rate-limit configuration and storage status                                       |

**Legacy commands**

| Command                       | Description                                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| `/sync <link> [--forward]`    | Sync historical messages to the current chat; restricted messages are forwarded via Takeout inline |
| `/syncrestrictedmsg <link>`   | Re-export only restricted messages to the current chat via Takeout                                 |
| `/monitor <link> [--forward]` | Same as `/jt`: monitor new messages and forward to the current chat                                |
| `/list`                       | Task management: pause / resume / delete / clear                                                   |
| `/clone [link]`               | Open the clone wizard                                                                              |

Sending a `t.me/...` link in a private chat with the bot triggers parsing and forwarding.

## Source Access Requirements

| Source Type             | UserBot for `/sync`                | UserBot for `/monitor`                | UserBot for `/syncrestrictedmsg`       |
| ----------------------- | ---------------------------------- | ------------------------------------- | -------------------------------------- |
| Public channel / group  | Usually no membership required     | **Must be a member**                  | Must be a member (Takeout requires it) |
| Private channel / group | Must be a member with read access  | **Must be a member with read access** | Must be a member with read access      |
| Forum topic             | Must have read access to the group | **Must be a member**                  | Must have read access to the group     |

- `/monitor` validates UserBot membership and access before creating a task; the task is rejected if requirements are not met.
- `/syncrestrictedmsg` uses Telegram's Takeout API, which requires UserBot to be a member of the source chat. It scans all messages first, then opens a Takeout session to re-export only the restricted ones.
- Bot always handles target-side delivery; source-side readability depends on whether the source is public.

## Forwarding Strategy

Public and private channels share **one degradation order** — it only drops a tier when the
cheaper one is not possible:

| Tier | What happens | Cost | Requires |
| --- | --- | --- | --- |
| **1. Bot direct** | Native forward / `drop_author` forward / send via the media reference Bot fetched | one server-side op, no local bytes | Bot can read source and post to target |
| **2. UserBot forward** | UserBot calls `forward_messages` (`drop_author` in copy mode) | one server-side op, no local bytes | Bot cannot read source, but UserBot is a member of both source and target |
| **2b. UserBot read + Bot write** | UserBot fetches the message, Bot posts it | no download, one Bot write | UserBot can read source but cannot post to target |
| **3. Download & re-upload** | UserBot downloads, Bot re-uploads | slowest, disk + bandwidth bound | source is `noforwards` / media reference expired |
| **4. Failure marker** | Posts `#fail2forward` with the source link | — | everything else failed |

> **Private channels no longer download by default.** If the UserBot is inside the source
> and has posting rights in the target, tier 2 handles it: Telegram moves the message
> server-side, so throughput depends on RTT rather than file size — and files above the
> upload limit go through fine.
>
> **Sender identity changes:** in `copy` mode tier 1 posts as the **Bot**, tier 2 posts as the
> **UserBot** (`drop_author` hides "forwarded from", but the sender is the UserBot). Set
> `transfer.userbot_forward: false` if posts must be authored by the Bot.
>
> **Capability cache:** "Bot cannot read this source" and "UserBot cannot post to this target"
> are learned once and reused for `transfer.probe_ttl_seconds` (default 30 min), so the clone
> stops hammering doomed permission errors — that alone is a large speed-up for private sources.
>
> **Note:** "Strategy 1 succeeded" in logs means the first tier succeeded — in `copy` mode this
> may internally use `send_message` / `send_file`. Comment links (`?comment=`) point to the
> linked discussion group, which may be private even if the original post is public.

## Docker Deployment

```bash
# 1. Copy and configure settings
cp config.example.yaml config.yaml

# 2. Interactively initialize UserBot session (Required on first run)
docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000

# 3. Build and start the bot service in the background
docker compose up -d --build
```

Volume mounts:

| Host            | Container          | Contents                                                        |
| --------------- | ------------------ | --------------------------------------------------------------- |
| `./config.yaml` | `/app/config.yaml` | Configuration (contains credentials)                            |
| `./data`        | `/app/data`        | **SQLite `bot.db` (clone progress / tasks), `bot.db-wal`, `backups/`, `proxy.json`** |
| `./sessions`    | `/app/sessions`    | Bot / UserBot login sessions                                    |

### Auto-update (no SSH needed)

`docker-compose.yaml` ships a `watchtower` service: it polls GHCR every 10 minutes and recreates
`tg-sync-bot` with the newly built image (only containers labelled
`com.centurylinklabs.watchtower.enable=true`, i.e. the bot). Run `docker compose up -d` once and a
`git push` is all it takes afterwards. Details: [docs/auto-update.md](docs/auto-update.md).

### Data persistence & backup

Clone cursors, tasks and message maps live in SQLite (`/app/data/bot.db`). As long as `./data`
is bind-mounted, pulling a new image and recreating the container keeps everything.

```bash
# Startup log reports where the DB lives and whether it survives image upgrades
Database ready · /app/data/bot.db (existing, 96.0 KB, persisted)

# Online snapshot (safe while the bot is running)
docker compose exec bot python scripts/backup_db.py --keep 14
```

- WAL is enabled by default: `docker stop`, container recreation and power loss won't corrupt the DB;
- Menu button "💾 备份数据库" snapshots to `data/backups/`, keeping the latest 7 by default;
- Custom location: `storage.db_path` or `TG_DB_PATH` — remember to add the matching volume;
- Full guide (Synology mapping, restore, migration, troubleshooting): [docs/storage.md](docs/storage.md).

## Important Notes

- **Restricted messages are sent as UserBot**: Takeout media references are bound to UserBot; Bot cannot use them directly.
- `forward` mode preserves native forwarding semantics; `copy` mode is more compatible but may trigger rate limits faster
- For private sources, UserBot must be a member with read access — otherwise strategies 2 and 3 will both fail
- Forum topic forwarding relies on `target_topic_id`; insufficient permissions on the target side will cause delivery failures
- If you encounter frequent `FloodWait` errors, increase the `rate_limit` parameters in your config
- Albums are sent as a group first; on failure, the bot falls back to per-message forwarding

## Logging & Troubleshooting

| Logger                             | Purpose                                        |
| ---------------------------------- | ---------------------------------------------- |
| `tg_forward_bot.handlers`          | Command and private-chat parsing entry point   |
| `tg_forward_bot.link_parser`       | Link resolution and discussion group discovery |
| `tg_forward_bot.forwarder`         | Strategy execution and fallback                |
| `tg_forward_bot.syncer`            | Historical sync progress                       |
| `tg_forward_bot.restricted_syncer` | Restricted message Takeout sync                |
| `tg_forward_bot.monitor`           | Real-time monitoring events                    |

For detailed operational guidance, see [`docs/operations.md`](docs/operations.md) and [`docs/architecture.md`](docs/architecture.md).
