<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-AGPL--3.0-green" alt="License">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Version-1.7.0-orange" alt="Version">
</p>

<h1 align="center">🛡️ PG-Limiter</h1>

<p align="center">
  <b>High-Performance IP Connection Limiter for <a href="https://github.com/PasarGuard/panel">PasarGuard</a> Panel</b>
  <br><br>
  Real-time SSE multi-node connection monitor, Telegram Forum Topics integration,<br>
  Subnet grouping, High Trust scoring, in-process caching, and zero-false-positive punishment system.
</p>

---

## 📑 Table of Contents

- [Features](#-features)
- [Requirements](#-requirements)
- [Installation](#-installation)
- [Management Commands](#-management-commands)
- [Telegram Bot & Topics](#-telegram-bot--topics)
- [Key Systems & Architecture](#-key-systems--architecture)
  - [3-Cycle Ban Condition](#1-3-consecutive-scans-verification)
  - [Subnet Grouping (/24)](#2-subnet-ip-grouping-24)
  - [High Trust Mode](#3-high-trust-mode)
  - [CDN & Cloudflare Support](#4-cdn-mode--x-forwarded-for-xff)
  - [Fast User Sync & In-RAM Cache](#5-fast-parallel-user-sync)
  - [IP Source: Logs or Panel API](#6-ip-source-node-logs-sse-or-panel-api)
- [Configuration](#-configuration)
- [CLI Interface](#-cli-interface)
- [REST API](#-rest-api)
- [Disable Methods](#-disable-methods)
- [Caching](#-caching)
- [Project Architecture](#-project-architecture)
- [FAQ](#-faq)
- [License & Credits](#-license--credits)

---

## ✨ Features

### 🔒 Core Protection
| Feature | Description |
|---------|-------------|
| 🔒 **IP Limiting** | Limit concurrent connections per user globally or with special per-user limits |
| 📊 **Real-time SSE Monitoring** | SSE-based live log streaming across all connected PasarGuard nodes |
| 🛰️ **Switchable IP Source** | Collect connected IPs from node logs (SSE) or from the panel API, switchable live from Telegram |
| 🛡️ **3-Cycle Verification** | Monitors violations across 3 consecutive scans before penalty, eliminating false positives |
| 🌐 **Subnet Grouping (/24)** | Consolidates dynamic cellular carrier IPs into single `/24` subnets |
| ⭐ **High Trust Mode** | Dynamically scores long-term stable users to prevent accidental disconnects |
| ☁️ **CDN & XFF Support** | Extracts genuine client IPs behind Cloudflare and reverse proxies |
| 📁 **Group-based Disable** | Temporarily moves violators to a restricted group instead of full account disabling |
| 🔄 **Auto-Recovery** | Automatically restores users once penalty durations expire |

### 🤖 Telegram Control & Dispatcher
| Feature | Description |
|---------|-------------|
| 📌 **Forum Topics** | Routes specific notifications to dedicated Supergroup Forum topics |
| ⚡ **Priority Dispatcher** | Rate-limited message queue with guaranteed delivery and error recovery |
| 🎛️ **Full Interactive UI** | Control settings, whitelist, special limits, nodes, and backups via inline buttons |
| 📝 **Audit Logging** | Preserves violation and disable message history for accounting |
| 💾 **Automated Backups** | Scheduled periodic backups sent directly to your Telegram backup topic |

### ⚡ Performance & Data Management
| Feature | Description |
|---------|-------------|
| 🚀 **Fast Parallel Sync** | Fetches 20,000+ users across pages in parallel with native SQLite bulk upsert |
| 🧠 **0ms In-RAM Lookups** | Zero-latency pre-computed RAM cache for instantaneous user limit evaluation |
| 🏎️ **In-Process Caching** | One bounded cache per data type, with no external cache service to run |
| 💿 **Async SQLite Engine** | Modern WAL-mode async database with automated Alembic migrations |
| 🐳 **Multi-Arch Docker** | Automated CI/CD builds for `linux/amd64` and `linux/arm64` |

---

## 📋 Requirements

- **Docker** and **Docker Compose** (installed automatically by the installer)
- **PasarGuard Panel** with API-key authentication; grant the key `nodes.logs` for SSE logs or `nodes.stats` for API mode, plus the permissions in the [panel API-key guide](docs/PANEL_API_KEY.md)
- **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))

---

## 🚀 Installation

### Quick Install with Docker (Recommended)

Run the one-line installer:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/JavadMoqaddam/PG-Limiter/v1.7.0/pg-limiter.sh) install
```

Or download and run step-by-step:

```bash
# Download the installer
curl -fsSL https://raw.githubusercontent.com/JavadMoqaddam/PG-Limiter/v1.7.0/pg-limiter.sh -o /tmp/pg-limiter.sh

# Run installation
sudo bash /tmp/pg-limiter.sh install
```

The installer will:
1. Check and install Docker & Docker Compose if missing.
2. Setup directories at `/etc/opt/pg-limiter/` and `/var/lib/pg-limiter/`.
3. Interactively prompt for the panel domain, panel API key and Telegram bot token.
4. Pull the multi-arch Docker image for the release the installer belongs to (`ghcr.io/javadmoqaddam/pg-limiter:<version>`).
5. Install the global `pg-limiter` command line tool and start the stack.

---

## 🕹️ Management Commands

Manage your instance anytime with the `pg-limiter` CLI tool:

```bash
pg-limiter start      # Start the service
pg-limiter stop       # Stop the service
pg-limiter restart    # Restart the service
pg-limiter status     # Show service & container status
pg-limiter logs       # View live logs (follow mode)
pg-limiter update     # Update to the latest release & migrate DB automatically
pg-limiter backup     # Create a manual backup zip
pg-limiter restore FILE  # Validate, stop service, and apply a backup offline
pg-limiter config     # Edit environment configuration (.env)
pg-limiter uninstall  # Remove PG-Limiter
```

Telegram `/restore` validates and stages uploads; it does not change live data. See [backup and offline restore](docs/BACKUP_RESTORE.md) for the snapshot guarantee, database path selection, and legacy compatibility.

---

## 🤖 Telegram Bot & Topics

The bot features a comprehensive menu structure with support for Telegram Supergroup Forum Topics:

```
🏠 Main Menu
├── ⚙️ Settings      → Bot settings, topics, subnet grouping, CDN mode
├── 🎯 Limits        → General & special user limit management
├── 👥 Users         → Exception whitelist, disabled users & manual enable
├── 📡 Monitoring    → Live active violations & connection monitor
├── 📊 Reports       → Protocol, node usage & IP history reports
└── 👑 Admin         → Manage authorized Telegram admins
```

### 📌 Forum Topics Setup
PG-Limiter can organize notifications across dedicated Forum topics in a Telegram Supergroup:

| Topic Type | Description |
|------------|-------------|
| ⚠️ **Warnings** | Real-time warnings when a user exceeds connection limits |
| 🚫 **Disable / Enable** | Critical disable notifications with instant `[✅ Enable]` action buttons |
| 📡 **Monitoring & Status** | Node connection statuses, heartbeat logs, and sync stats |
| 💾 **Backups** | Automated and manual database/config backup archives |
| 📊 **Daily Report** | Previous calendar day’s users actually banned, grouped by applied punishment step |
| ♾️ **No Limit** | Notifications for unlimited / exempted users |

Daily Report sends one brief message at **00:00** for the previous complete calendar
day, using the current `TZ` environment setting. An unset `TZ` uses UTC; an invalid
explicit zone pauses reporting with a service diagnostic. Changes to the running
process environment reschedule the next midnight within 60 seconds. After editing
a deployment `.env` file, restart the process or recreate the container so it receives
the new environment; log-source timezone settings do not control this schedule.

The report counts distinct users per actual applied step and once in the total,
with a separate Plain ban row when escalation is disabled. A user can appear in
several step counts, so their sum can exceed the total. Warnings and failed disables
are excluded. Reports contain no usernames or IPs and fit one message of at most
2000 UTF-16 units; exceptionally many steps are combined as user/step counts.
Zero-ban days report `Total: 0 users`.

Configure the Daily Report topic in Forum Topics settings. Topics off produces no
new report or disabled-day backlog. Restart recovery considers the most recent
eligible missed day. Pending fact writes delay its summary, and known delivered
receipts prevent replay after topic-cache or destination changes. An uncertain
panel action or Telegram send remains unresolved instead of inventing a successful
outcome or automatically sending a possible duplicate. Run one limiter daemon per
database. New backups include the pending fact outbox; valid older backups remain
restorable and remove any newer outbox before the restored database starts.

---

## 💡 Key Systems & Architecture

### 1. 3 Consecutive Scans Verification
To prevent false-positive bans caused by temporary network glitches, VPN reconnects, or carrier handovers:
* **Scan 1:** System flags over-limit usage and places user under monitoring (`consecutive_violations = 1`).
* **Scan 2:** Violation persists; warning updated (`consecutive_violations = 2`).
* **Scan 3:** Third consecutive violation confirms abuse; user is disabled/moved according to punishment rules.
* **Auto-Purge:** If user traffic normalizes or user disconnects at any point before Scan 3, monitoring state is instantly cleared.

### 2. Subnet IP Grouping (/24)
Mobile network providers (Irancell, MCI, Rightel, etc.) often assign multiple IPs from the same `/24` block during a single session. Enabling Subnet Grouping aggregates IP addresses sharing the same `/24` subnet prefix as a single connection.

### 3. High Trust Mode
Users with established, stable connection histories receive trust score increments. High Trust Mode provides dynamic tolerance for trusted users while strictly enforcing limits on volatile or unverified connections.

### 4. CDN Mode & X-Forwarded-For (XFF)
When running proxy frontends or Cloudflare CDN layers, PG-Limiter extracts the real client IP from `X-Forwarded-For` / `CF-Connecting-IP` headers rather than the CDN edge IP.

### 5. Fast Parallel User Sync
Synchronizes all users and statuses from PasarGuard API using validated parallel pagination (up to 10 concurrent requests). Native `INSERT ... ON CONFLICT DO UPDATE` commits 500-user batches so configuration and enforcement writers can run between batches. Cleanup confirms each absent user's current detail response before deletion and preserves local rows changed during synchronization. Failed or incomplete list responses never trigger cleanup; uncertain deletion checks retain those users and report an incomplete sync. Existing users retain their local limits, whitelist and punishment state. The factual metadata cache refreshes after committed writes for fast policy evaluation.

### 6. IP Source: Node Logs (SSE) or Panel API
The connected IPs of each user can come from either of two sources, switchable at runtime from **Settings → 🛰️ IP Source (Logs / API)** with no restart:

| | 📜 **Node Logs (SSE)** — default | 🛰️ **Panel API** |
|---|---|---|
| Transport | One persistent SSE stream per node | `GET /api/node/online_stats/{id}/ip`, once per online user per cycle |
| Timing | Original Xray printed timestamps checked against shared Last seen at evaluation | Original Xray IP timestamps checked against the same Last seen threshold |
| Inbound detail | Real inbound name per connection | Single sentinel inbound (`API`) |
| Requires | API key with `nodes.logs`, reachable SSE, explicit Xray process timezones and independently ready nodes after conservative warm-up | API key with `nodes.stats` and complete evidence from relevant enabled nodes |

Both modes publish source-specific snapshots through the shared `ACTIVE_USERS` structure. Existing device counting, subnet/CDN/high-trust grouping, trust scoring and punishment continue to apply, with configurable `CHECK_INTERVAL` and `MAX_WARNING_COUNT`. API observations carry no real inbound names; inbound-only monitoring requires Logs.

**Last seen applies to both sources.** The Telegram setting uses the existing `api_ip_freshness` database row and `API_IP_FRESHNESS` ENV default; no separate Logs setting is required. `CHECK_INTERVAL` controls scan cadence and warning timing. An observation qualifies only while `0 <= evaluation time - original source timestamp < Last seen`, including after slow preparation. Source-opening timestamps do not establish continuous bytes or an idle open connection. SSE heartbeats and replay delivery do not make old observations fresh.

When Last seen exceeds the cadence, Logs keeps eligible observations across successive scans without renewing their timestamps. Source/settings changes and recovered nodes require conservative warm-up of `max(CHECK_INTERVAL, Last seen)`.

**How API mode keeps a cycle safe:**

* **Candidate narrowing.** `GET /api/users` uses `status=active` and `online=true`, independently of `CHECK_INTERVAL`. Include group IDs are sent together; empty Include selects nobody. Exclusions, administrator filters and exceptions still apply. Group Limits set limits and never replace the Group Filter.
* **Exact Last seen threshold.** Edit a positive whole number of seconds T in **Settings → IP Source → Last seen**. Both API and Logs use that value exactly, with no 30-second buffer. A long-lived connection can have an old timestamp; this is recent source evidence, not a byte-traffic meter. API and Logs counts can differ.
* **Unknown evidence protection.** Failed requests, malformed bodies, invalid timestamps and missing expected node responses protect affected users from warnings, punishment and false clearing. Incomplete candidate/node lists or a relevant enabled node in `error`/`connecting` skip the whole cycle, including an empty candidate list, preserving existing warnings. Explicitly operator-disabled nodes are excluded. The configurable coverage gates (user coverage defaults to **80%**) can skip collection; a high answer rate never makes missing evidence known. Healthy empty maps, valid expired observations and confirmed deleted users can clear warnings.
* **Auto-fallback.** With automatic fallback enabled, exactly two consecutive unsuccessful completed API collection cycles persist the source as Logs and attempt one Telegram alert. A complete successful cycle, including a genuinely empty one, resets the streak. Logs then waits for its timezone/readiness requirements and conservative warm-up.
* **Per-inbound CDN grouping is inactive** in API mode because the panel reports no inbound names — use **CDN Nodes** (per-node) instead of CDN Inbounds.

Switching to API mode runs a pre-flight probe against a real online user first, and refuses to switch if the endpoint is not reachable. **Settings → 🛰️ IP Source → 📊 Last API Cycle** separates successful requests, users missing required node evidence, expected nodes reporting across responses, globally absent nodes and elapsed time. HTTP 200 alone does not prove a complete per-user node map. Missing-node IDs across users are a union, not a list missing from every user; aggregate node coverage is a separate measure. If collection stops before node-map evaluation, reporting and absence remain unknown. After switching to Logs, the API report is historical.

Logs preserves Xray's original printed process-local time and rejects retained replay records across reconnects. Configure `LOG_NODE_TIMEZONES` and/or an explicit `LOG_TIMEZONE` for the Xray process/container; host or panel timezone alone does not establish it. Unknown zones, fleet or transport readiness preserve warnings while enforcement waits. A source/settings change or transport recovery requires `max(CHECK_INTERVAL, Last seen)` of warm-up before Logs resumes. See [Panel API IP source](docs/API_IP_SOURCE.md) and [Node Logs source and readiness](docs/LOG_SOURCE.md) for configuration, recovery and protocol limits.

### 7. Device Counting Modes
Choose the enforcement measure at runtime from **Settings → 🧮 Device Counting**:

| Mode | What counts as one device |
|---|---|
| **Per Device (by inbound)** — default | One client IP (or grouped subnet) on one inbound |
| **Per IP** | One accepted source IP (or configured grouping), regardless of inbound or node |
| **Per Inbound** | One distinct inbound tag with eligible recent source evidence, regardless of IPs |
| **Per Node** | One known connected monitored node with accepted active user connections |

In Per Inbound mode, ten IPs on one inbound count as **1**, while one IP on three inbounds counts as **3**. The same inbound tag across several nodes counts once. Tags are trimmed and case-sensitive; disabled nodes, blank tags and the reserved `Unknown` placeholder contribute nothing. Without inbound information, the count is **0** and never falls back to the number of IPs. CDN, subnet and High Trust grouping do not change this count.

In Per IP mode, the same accepted IP on two fresh valid nodes counts as **1**; two accepted IPs count as **2** unless configured subnet or CDN grouping combines them. High Trust inbound grouping does not merge Per IP addresses. Registered panel-node addresses are filtered. Other relay addresses can appear in the source IPs shown by the UI, so Per IP does not establish the number of physical client devices.

Per Inbound uses accepted log events still within Last seen when evaluated. These recent openings do not prove ongoing bytes. It requires **Node Logs (SSE)**: switch the IP source to logs before selecting it, and choose Per Device, Per IP or Per Node before switching to the panel API. If an incompatible API + Per Inbound combination is persisted externally, collection and enforcement pause while existing samples and warning counters are preserved.

Per Node works with **Logs and API**. Several IPs or inbounds on one eligible node count as **1**; the same IP on two eligible nodes counts as **2**, including CDN traffic. CDN, subnet and High Trust grouping do not change this count. Nodes disabled in the limiter or panel, unknown IDs and malformed IDs contribute nothing. An integer ID of zero is valid when it belongs to the authoritative node fleet. Bare IPs cannot establish a node count. Accepted connections still obey country, policy and source freshness checks; missing fleet readiness or unknown user evidence preserves warnings and pauses the affected enforcement instead of clearing them as zero usage.

With Logs, Per Node and Per Inbound can continue on independently ready nodes if their observations alone prove a violation. Missing-node data never clears an absent or below-limit user's warning. Recovered nodes require `max(CHECK_INTERVAL, Last seen)` of warm-up before rejoining counts. General reports included-node health and coverage transitions; Status & Reports preserves the actual ready/required coverage of completed scans. See [Logs readiness](docs/LOG_SOURCE.md).

All counting modes use the dynamic database setting `device_count_mode` (`device`, `ip`, `inbound` or `node`); changing it requires no restart or schema migration.

---

## ⚙️ Configuration

Telegram, CLI, and API settings share the same stored configuration. Saved changes
take effect on the next usable monitoring scan without restarting the daemon.
User limits follow special limit → highest matching group limit → general limit.
Whitelist, group, and admin filters apply to both ongoing and expired warnings.

Before punishment, the limiter checks the user's current groups, owner, eligibility,
and limit again. If settings or the IP source change during a scan, or current user
facts are unavailable, punishment waits and existing warnings are retained. A user
who becomes whitelisted or falls outside the configured filters also keeps their
existing warning until they can be assessed within the monitoring scope again.

### Environment Variables (`/etc/opt/pg-limiter/.env`)

```bash
# Panel Settings (Required)
PANEL_DOMAIN=panel.example.com:8443
PANEL_API_KEY=your_panel_api_key

# Telegram Bot (Required)
BOT_TOKEN=123456789:ABC-YOUR-BOT-TOKEN
ADMIN_IDS=5917047651

# Limiter Settings
GENERAL_LIMIT=2
CHECK_INTERVAL=60
TIME_TO_ACTIVE_USERS=1800
COUNTRY_CODE=IR

# Daily Report calendar timezone (00:00; unset defaults to UTC)
TZ=UTC
```

The panel API key is the sole panel credential for builds containing this change.
REST requests, SSE log streams and startup probes send it in `X-Api-Key`.
Panel username/password login is unsupported. Changing `PANEL_DOMAIN` or
`PANEL_API_KEY` requires restarting the process or recreating its container.
See the [panel API-key guide](docs/PANEL_API_KEY.md) for permission scopes,
upgrading an existing `.env` and TLS settings. The released `v1.7.0` installer
and image linked above predate this change; deploy a build containing it before
migrating your credentials. The optional management API keeps its separate
`API_USERNAME` and `API_PASSWORD` authentication.

---

## 🖥️ CLI Interface

Manage limits and users directly via CLI inside or outside Docker:

```bash
# User limits
python cli_main.py user list
python cli_main.py user add USERNAME 3
python cli_main.py user delete USERNAME

# Whitelist exceptions
python cli_main.py except list
python cli_main.py except add USERNAME
python cli_main.py except delete USERNAME

# Disabled users
python cli_main.py disabled list
python cli_main.py disabled enable USERNAME
python cli_main.py disabled enable-all
```

---

## 🌐 REST API

An optional REST API server is available for custom dashboards and automation:

```bash
python api_server.py
```

Runs on port `8080` by default. Swagger documentation available at `http://localhost:8080/docs`.

---

## 🚫 Disable Methods

1. **Status-based (Default):** Sets user status to `disabled` in PasarGuard panel.
2. **Group-based:** Preserves original user groups and temporarily moves user to a restricted fallback group ID until re-enabled.

---

## 🚀 Caching

| Cache Item | TTL | Purpose |
|------------|-----|---------|
| **Access Tokens** | 30 min | Panel API authentication |
| **Node List** | 1 hour | Active PasarGuard SSE nodes |
| **Configuration** | 5 min | Dynamic runtime parameters |
| **User Metadata** | 1 min | Pre-computed limits held in RAM |

ISP lookups run only for successful disable notifications, once per raw IP, with
no memory/database cache or subnet request aggregation. Warnings and reports do
not request ISP information. `/24` device grouping remains available; the former
ISP-dependent `/16` mode now uses `/24`. See [ISP notifications](docs/ISP_NOTIFICATIONS.md).

---

## 🏗️ Project Architecture

```
PG-Limiter/
├── limiter.py                  # Main daemon loop & TaskGroup supervisor
├── start.sh                    # Container bootstrap & migration runner
├── pg-limiter.sh               # CLI management utility
├── docker-compose.yml          # Production multi-container definition
├── Dockerfile                  # Multi-arch lightweight container image
│
├── telegram_bot/               # Telegram Bot Layer
│   ├── main.py                 # Bot initialization & command router
│   ├── dispatcher.py           # Priority message queue & rate limiter
│   ├── topics.py               # Supergroup Forum Topics manager
│   ├── keyboards.py            # Inline UI layouts
│   ├── send_message.py         # Message dispatching helpers
│   └── handlers/               # Modular bot controllers
│       ├── settings.py         # Settings, topics, subnet & CDN controllers
│       ├── limits.py           # General & custom limit handlers
│       ├── users.py            # User whitelist & disable controllers
│       ├── monitoring.py       # Live connection monitor
│       ├── reports.py          # Connection and IP history reports
│       └── backup.py           # Backup & restore handlers
│
├── utils/                      # Core Business Logic & Workers
│   ├── check_usage.py          # IP analysis, limits resolution & enforcement
│   ├── ip_source_api.py        # API-mode IP collector (panel online-stats)
│   ├── user_sync.py            # Fast parallel panel user synchronization
│   ├── isp_detector.py         # Fresh per-IP lookups for disable notifications
│   ├── punishment_system.py    # Tiered punishment duration manager
│   └── warning_system/         # 3-cycle consecutive tracking & trust scoring
│
└── db/                         # Database & Persistence Layer
    ├── database.py             # Async SQLite engine & connection pool
    ├── models.py               # SQLAlchemy ORM models
    ├── crud/                   # High-performance DB operations
    └── migrations/             # Alembic versioned schema migrations
```

---

## ❓ FAQ

<details>
<summary><b>Does PG-Limiter ban users who disconnect during a scan?</b></summary>
No. With the 3-cycle consecutive verification system, if a user disconnects or normalizes their connection before reaching 3 consecutive scans, their violation counter is automatically cleared with zero penalty.
</details>

<details>
<summary><b>How does Subnet Grouping work?</b></summary>
When Subnet Grouping is enabled, multiple connections originating from the same <code>/24</code> subnet (common in mobile cellular carrier networks) are recognized as a single device session.
</details>

<details>
<summary><b>When should I switch the IP source to the panel API?</b></summary>
Use API mode when node logs are unavailable or too noisy to stream reliably. It queries <code>online_stats</code> once per online user per cycle; panel load depends on candidate count, node RPC latency and concurrency. It needs a panel API key with the <code>nodes.stats</code> permission. With automatic fallback enabled, two unsuccessful collection cycles switch to Logs, including cycles whose HTTP requests succeed but whose per-user node evidence is incomplete. Grant <code>nodes.logs</code> too when using automatic fallback.
</details>

<details>
<summary><b>How do I update PG-Limiter?</b></summary>
Simply run <code>pg-limiter update</code>. It pulls the latest Docker image, applies any pending database migrations automatically, and restarts the service with zero downtime.
</details>

---

## 📄 License & Credits

This project is licensed under the **AGPL-3.0 License** - see the [LICENSE](LICENSE) file for details.

Based on [V2IpLimit](https://github.com/houshmand-2005/V2IpLimit) by [houshmand-2005](https://github.com/houshmand-2005), redesigned and enhanced for the PasarGuard community.
