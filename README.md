<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-AGPL--3.0-green" alt="License">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Version-1.3.0-orange" alt="Version">
</p>

<h1 align="center">🛡️ PG-Limiter</h1>

<p align="center">
  <b>High-Performance IP Connection Limiter for <a href="https://github.com/PasarGuard/panel">PasarGuard</a> Panel</b>
  <br><br>
  Real-time SSE multi-node connection monitor, Telegram Forum Topics integration,<br>
  Subnet grouping, High Trust scoring, fast Redis/RAM caching, and zero-false-positive punishment system.
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
- [Redis Caching](#-redis-caching)
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
| 🏎️ **Redis Caching** | High-performance distributed caching with automatic in-memory fallback |
| 💿 **Async SQLite Engine** | Modern WAL-mode async database with automated Alembic migrations |
| 🐳 **Multi-Arch Docker** | Automated CI/CD builds for `linux/amd64` and `linux/arm64` |

---

## 📋 Requirements

- **Docker** and **Docker Compose** (installed automatically by the installer)
- **PasarGuard Panel** (latest version with SSE logs enabled, or an account with `nodes:stats` for API mode)
- **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))
- **Redis** (included in the default Docker Compose stack)

---

## 🚀 Installation

### Quick Install with Docker (Recommended)

Run the one-line installer:

```bash
sudo bash <(curl -sSL https://raw.githubusercontent.com/JavadMoqaddam/PG-Limiter/main/pg-limiter.sh) install
```

Or download and run step-by-step:

```bash
# Download the installer
curl -sSL https://raw.githubusercontent.com/JavadMoqaddam/PG-Limiter/main/pg-limiter.sh -o /tmp/pg-limiter.sh

# Run installation
sudo bash /tmp/pg-limiter.sh install
```

The installer will:
1. Check and install Docker & Docker Compose if missing.
2. Setup directories at `/etc/opt/pg-limiter/` and `/var/lib/pg-limiter/`.
3. Interactively prompt for panel credentials and Telegram bot tokens.
4. Pull the latest multi-arch Docker image (`ghcr.io/javadmoqaddam/pg-limiter:latest`).
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
pg-limiter restore    # Restore configuration & database from backup
pg-limiter config     # Edit environment configuration (.env)
pg-limiter uninstall  # Remove PG-Limiter
```

---

## 🤖 Telegram Bot & Topics

The bot features a comprehensive menu structure with support for Telegram Supergroup Forum Topics:

```
🏠 Main Menu
├── ⚙️ Settings      → Bot settings, topics, subnet grouping, CDN mode
├── 🎯 Limits        → General & special user limit management
├── 👥 Users         → Exception whitelist, disabled users & manual enable
├── 📡 Monitoring    → Live active violations & connection monitor
├── 📊 Reports       → ISP, protocol, node usage & IP history reports
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
| ♾️ **No Limit** | Notifications for unlimited / exempted users |

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
Synchronizes 20,000+ users from PasarGuard API using parallel pagination batches (up to 10 concurrent requests) and writes them to SQLite in a single transaction via native `INSERT ... ON CONFLICT DO UPDATE`, caching metadata and limits in RAM for $O(1)$ 0ms lookups.

### 6. IP Source: Node Logs (SSE) or Panel API
The connected IPs of each user can come from either of two sources, switchable at runtime from **Settings → 🛰️ IP Source (Logs / API)** with no restart:

| | 📜 **Node Logs (SSE)** — default | 🛰️ **Panel API** |
|---|---|---|
| Transport | One persistent SSE stream per node | `GET /api/node/online_stats/{id}/ip`, once per online user per cycle |
| Timing | Continuous, accumulated between cycles | One instant snapshot taken immediately before enforcement |
| Inbound detail | Real inbound name per connection | Single sentinel inbound (`API`) |
| Requires | Node logs reachable over SSE | Panel account with the `nodes:stats` permission |

Both modes write into the same `ACTIVE_USERS` structure, so device counting, subnet/CDN/high-trust grouping, the 3-cycle warning system, trust scoring and punishment behave identically.

**How API mode keeps a cycle safe:**
* **Candidate narrowing.** `GET /api/users` is filtered panel-side by the group IDs from Group Limits / Group Filter, by `status=active`, and by an online-freshness window equal to `CHECK_INTERVAL + 30s`, so only the handful of currently-online monitored users is queried.
* **Coverage gate.** If fewer than `api_ip_min_coverage` (default **80%**) of the candidates answered, the whole cycle is skipped rather than enforced on partial data — an under-covered snapshot could otherwise clear the counters of real offenders.
* **Never a false positive.** A snapshot can only report *fewer* IPs than continuous log streaming, never more, and a failed candidate query leaves the previous state untouched instead of wiping pending warnings.
* **Auto-fallback.** After 3 consecutive cycles with no usable data (for example a missing `nodes:stats` permission) the IP source reverts to logs automatically and a Telegram alert is sent.
* **Per-inbound CDN grouping is inactive** in API mode because the panel reports no inbound names — use **CDN Nodes** (per-node) instead of CDN Inbounds.

Switching to API mode runs a pre-flight probe against a real online user first, and refuses to switch if the endpoint is not reachable. **Settings → 🛰️ IP Source → 📊 Last API Cycle** shows the last cycle's candidate narrowing, coverage, 403/404 counts and duration.

---

## ⚙️ Configuration

### Environment Variables (`/etc/opt/pg-limiter/.env`)

```bash
# Panel Settings (Required)
PANEL_DOMAIN=panel.example.com:8443
PANEL_USERNAME=admin
PANEL_PASSWORD=your_secure_password

# Telegram Bot (Required)
BOT_TOKEN=123456789:ABC-YOUR-BOT-TOKEN
ADMIN_IDS=5917047651

# Limiter Settings
GENERAL_LIMIT=2
CHECK_INTERVAL=60
TIME_TO_ACTIVE_USERS=1800
COUNTRY_CODE=IR

# Redis Cache (Included in default stack)
REDIS_URL=redis://redis:6379

# Timezone
TZ=Asia/Tehran
```

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

## 🚀 Redis Caching

| Cache Item | TTL | Purpose |
|------------|-----|---------|
| **Access Tokens** | 30 min | Panel API authentication |
| **Node List** | 1 hour | Active PasarGuard SSE nodes |
| **Configuration** | 5 min | Dynamic runtime parameters |
| **Subnet & ISP Info** | 7 days | IP-to-carrier resolution cache |
| **User Metadata** | 1 min | RAM/Redis synchronization |

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
│       ├── reports.py          # Detailed connection & ISP reports
│       └── backup.py           # Backup & restore handlers
│
├── utils/                      # Core Business Logic & Workers
│   ├── check_usage.py          # IP analysis, limits resolution & enforcement
│   ├── ip_source_api.py        # API-mode IP collector (panel online-stats)
│   ├── user_sync.py            # Fast parallel panel user synchronization
│   ├── isp_detector.py         # Subnet & ISP detection engine
│   ├── redis_cache.py          # Redis async client & Pub/Sub invalidation
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
Use API mode when node logs are unavailable or too noisy to stream reliably. It queries <code>online_stats</code> once per online user per cycle, so with a <code>CHECK_INTERVAL</code> of 180–300 seconds the panel load stays negligible. It needs a panel account with the <code>nodes:stats</code> permission; the bot pre-flights that before switching, and reverts to logs automatically if the endpoint stops answering.
</details>

<details>
<summary><b>How do I update PG-Limiter?</b></summary>
Simply run <code>pg-limiter update</code>. It pulls the latest Docker image, applies any pending database migrations automatically, and restarts the service with zero downtime.
</details>

---

## 📄 License & Credits

This project is licensed under the **AGPL-3.0 License** - see the [LICENSE](LICENSE) file for details.

Based on [V2IpLimit](https://github.com/houshmand-2005/V2IpLimit) by [houshmand-2005](https://github.com/houshmand-2005), redesigned and enhanced for the PasarGuard community.
