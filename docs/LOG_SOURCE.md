# Logs source configuration and readiness

Logs enforcement requires the timezone used by each node's **Xray process**. Xray prints local access-log timestamps without a UTC offset, and the node/panel stream supplies neither timezone metadata nor a history/live boundary. Configure verified values in the limiter environment, keyed by **panel node ID**:

```dotenv
# Illustrative IDs; replace with your actual panel node IDs and verified zones.
LOG_NODE_TIMEZONES='{"12":"Europe/Istanbul","34":"America/New_York"}'
# Optional explicit default for nodes without an override:
# LOG_TIMEZONE=UTC
# Maximum retained record fingerprints per node:
# LOG_REPLAY_CACHE_SIZE=10000
```

Both timezone settings are unset by default. A valid per-node override wins over the optional default. An invalid override cannot inherit that default. Use exact IANA zone names supported by the limiter's system timezone database, such as `Asia/Tehran`, `Europe/Berlin`, or `UTC`. Invalid JSON or capacity leaves enforcement waiting; a missing/invalid node zone makes that node's evidence unavailable. Environment changes require restarting the limiter; this does not change any host or container clock. Panel API collection is independent of these settings.

The VPS host timezone selected by `timedatectl` does not establish a container's timezone. The official node container setup does not automatically mount the host's `/etc/localtime` or forward its timezone. Verify the running Xray container/process environment, timezone files and effective time before configuring it. Do not infer a timezone from the VPS country, node IP, panel display, limiter host, or a similarly named container on another host. Several checked UTC containers establish only those containers' state, not the fleet's timezone.

If every monitored Xray process has been verified to use UTC, set `LOG_TIMEZONE=UTC` in the limiter's `.env`. With the standard installer this is `/etc/opt/pg-limiter/.env`; run `sudo pg-limiter config`, add the setting, then run `sudo pg-limiter restart` to recreate the container with the updated environment. With a manual Compose installation, edit its `.env` and run `docker compose up -d --force-recreate pg-limiter`. A plain `docker restart` retains the old container environment. Per-node overrides still take precedence, so correct any outdated `LOG_NODE_TIMEZONES` entries too. After recreation, allow healthy fleet/log transport for at least `max(CHECK_INTERVAL, Last seen)` before expecting enforcement.

## What counts

The limiter preserves the original printed timestamp and converts it to UTC. The existing Telegram **Settings → IP Source → Last seen** setting (`api_ip_freshness`, defaulted by `API_IP_FRESHNESS`) applies to both API and SSE/Logs. At evaluation time an accepted record qualifies only when `0 <= evaluation time - original source timestamp < Last seen`. The configured positive value is used exactly; `CHECK_INTERVAL` schedules scans and warning timing independently.

The timestamp parser supports `YYYY/MM/DD HH:MM:SS`, with optional one-to-six fractional digits; current Xray prints six. Different records with the same timestamp remain distinct. Both `>>` and `->` inbound separators remain supported. Receipt time, SSE heartbeats and replayed records never renew observation freshness.

An accepted record describes a recent connection opening. It does not establish continuous byte traffic or prove that an idle connection remains open. A long-lived transmitting flow can produce no new accepted record and therefore fall outside Last seen. This setting selects recent source evidence rather than measuring ongoing traffic.

Last seen can exceed the scan cadence. Eligible observations then remain in a rolling window across successive snapshots, keeping their original timestamps and expiring at the exact freshness boundary. For example, Last seen 180 with a 60-second cadence can keep an observation eligible for several scans without changing its timestamp. The same accepted IP on two fresh valid nodes counts as one in Per IP and two in Per Node. Two accepted IPs count as two in Per IP unless configured subnet or CDN grouping combines them. Registered panel-node addresses are filtered; external relay addresses can still appear in UI source IPs, so these counts do not establish physical client devices.

Full-record SHA-256 fingerprints are scoped per node and retained across stream reconnects, consumed snapshots and periodic task rebuilds. Repeated history cannot create a fresh observation or increment a connection count. Pending observations expire by their original timestamps. Changes to source, Last seen, check interval, timezone settings, cache capacity or limiter disabled-node settings retire the previous generation and require conservative warm-up for at least `max(CHECK_INTERVAL, Last seen)` before enforcement resumes. A panel node's disconnect, reconnect or transport gap retires that node's evidence independently; a new or recovered node needs its own warm-up. These transitions do not reset warning history or change the configured warning threshold, punishment, device counting, or probe policy.

A snapshot retains its observation bounds, required node IDs and independently ready node IDs through slow ISP and metadata preparation. Only captured ready nodes contribute observations. Freshness is revalidated and counts are recomputed locally before warning, punishment or warning-clearing decisions, so slow preparation cannot keep an expired observation eligible. Partial coverage still protects absent or below-limit users. Configuration changes and global faults invalidate the sample; a fault affecting a captured node removes only that node from the remaining decisions; a recovered node cannot enter an older snapshot. A parser prepared before its own node epoch changed cannot write observations after recovery.

## When enforcement waits

The IP Source settings and node status messages show Logs readiness. Nodes in the limiter's `disabled_nodes` list and panel nodes with status `disabled` or `limited` are excluded: they require no stream, timezone, timestamp or transport evidence, and their observations do not contribute to counting. An unknown, malformed or stale authoritative fleet blocks enforcement. An authoritative empty monitoring scope becomes valid empty evidence after the conservative warm-up; a missing fleet is never treated as empty.

An included node without a valid zone, panel connection, live transport or completed warm-up is unavailable independently of healthy nodes. All counting modes can continue positive enforcement when healthy observations alone exceed the current user limit. Partial coverage cannot clear warnings for absent or below-limit users, including expired warnings. For Per Device and Per IP, incomplete CDN observations use the larger of the ordinary distinct-device count and one if any CDN observation exists. Missing observations can suppress CDN buckets, so this conservative lower bound prevents false violations. Complete coverage retains normal CDN counting. Complete valid empty evidence can clear records normally.

SSE comment pings update transport liveness even without accepted traffic; they establish panel-to-limiter transport only, so panel node status is checked separately. After a known gap or reconnect, that node requires at least `max(CHECK_INTERVAL, Last seen)` of warm-up again. The first HTTP response or recovered heartbeat cannot certify the missing preceding period. Replay fingerprints survive this warm-up.

An otherwise usable accepted record with a malformed, non-finite, future, ambiguous daylight-saving, or nonexistent daylight-saving timestamp makes the affected node's interval unavailable. Timestamp validity is checked before preparation and at final admission; a backwards clock step during preparation cannot silently discard future evidence as known absence. A finite future timestamp protects its node until the original source timestamp plus Last seen. Known old history is harmless and is ignored. If no valid positive partial evidence is available, the cycle preserves warnings without warning, punishment or cleanup decisions.

Replay memory is bounded at **10000 records per node** by default. A source-time expiry index reclaims expired fingerprints even when timestamps arrive out of order. Its auxiliary entries are bounded to twice the retained fingerprint count; admission and expiry use heap operations, with occasional amortized rebuilding after live evictions. If capacity evicts a fingerprint that could still qualify in the current window, that node waits until the record's original timestamp is outside the window. Busy nodes may need a larger `LOG_REPLAY_CACHE_SIZE`; this is a memory/availability tradeoff, not a silent replay allowance. Automatic API fallback uses the same Logs readiness checks and warmup.

## General-topic health events

General retains the editable node connection summary and adds incident history:
included-node disconnects, missing/stale log transport, uncertain observations,
recovery in progress and restored readiness. Monitoring coverage
changes and the first completed scan are also reported. Repeated observations of
the same state do not produce another incident notification. Intentionally
excluded nodes, API-source stream closure, paused monitoring and scheduled stream
rebuilds do not generate node-outage incidents. Detection follows the existing
fleet and transport polling cadence.

Status & Reports shows the coverage captured by the last completed scan,
including ready/required node counts and missing IDs for partial scans. A
completed partial scan does not establish full-fleet health. General health
messages include node IDs, escaped names and fixed reason categories; they do not
include access logs, individual user activity or raw exceptions.

## Diagnosing repeated uncertainty

`timestamp or replay uncertainty in the current interval` can mean an unusable source timestamp or eviction of a still-current replay fingerprint. Known duplicate records and valid old history do not cause this wait. If faults continue, waiting one interval does not resolve it. The `Logs uncertainty diagnostics` log entry separates fixed timestamp error reasons, live cache eviction and capacity reduction, with affected panel node IDs, cache occupancy and the remaining uncertainty deadline. It prints aggregate metadata at most once per minute and contains no access records, usernames or IPs.

For an existing 1.7.0 container, copy `tools/diagnose_log_source.py` to the limiter host, for example `/tmp/diagnose_log_source.py`, and run:

```bash
sudo docker exec -i pg-limiter python - < /tmp/diagnose_log_source.py
```

The standalone probe reads only the relevant committed SQLite settings and briefly opens independent panel log streams. It imports no application modules, sends no Telegram messages, and does not change configuration, user data or the daemon's counters. The default probe has four concurrent streams and an overall three-minute deadline. Output contains node IDs, effective source timezone, fixed timestamp error counts, source-time differences and a conditional replay-capacity projection. A positive timestamp difference can indicate clock drift or an incorrect timezone setting. These potential access records precede country, CDN, node-IP and user-policy filtering. The probe observes its own streams and cache; its output cannot establish the daemon's cache occupancy or fault subtype.

The probe reports `freshness_seconds` separately from `CHECK_INTERVAL`. Its stale/recent classification and replay expiry use that freshness window. `projected_freshness_records_at_sample_rate` scales the sampled rate to Last seen for a conditional replay-capacity estimate; `projected_interval_records_at_sample_rate` is retained as a cadence-only count. Neither estimate measures the running daemon's cache occupancy. Legacy Auto is resolved read-only using the application's migration rule.

`probe_format_version=2` also measures buffered records before the stream started:

- `oldest_source_epoch`, `newest_source_epoch`, source event span and receipt ages include all successfully parsed potential access timestamps, including stale and future records. A negative age indicates a timestamp ahead of receipt. When no timestamp parses, ages are null. `newest_record_age_at_sample_end_seconds` uses the stream's finish time.
- `recent_history_records` counts raw receipts with a nonfuture timestamp less than the configured freshness window old but before this probe's start; it includes duplicates. `recent_distinct_records` fingerprints all records that are recent at receipt, including this history. `duplicate_records` therefore includes both recent history and post-start records. `sample_cache_scope` labels this independent cache explicitly.
- `live_distinct_records` and its rate/projection still require timestamps at or after probe start. A slow clock or delayed delivery can leave this count at zero despite records continuing to arrive. Buffered-history counts must not be multiplied by the interval as if they were a steady live rate.
- `sse_data_records`, `records_by_receipt_second` and `record_receive_span_seconds` cover all decoded, size-bounded SSE data records, including lines without potential access records. The 16 one-second buckets are bounded; the last bucket includes receipts from second 15 onward, and `receipt_bin_overflow_records` counts receipts from second 16 onward.

Probe fingerprints remain bounded by the smaller of configured capacity and 100000 entries. A probe eviction makes `sample_deduplication_complete=false`; repeated identities can then be counted again. The probe's arrival-ordered cache can also retain expired entries behind a live entry, so its occupancy/evictions are conservative diagnostics, not proof of live pressure in the daemon. Startup tails, clock lag, delivery lag and continuing activity require interpretation of ages, receipt progression and a controlled new connection together.

To focus on panel node IDs or extend each short sample, use `python - --nodes 24,32 --duration 15` in the same command. `configuration_unavailable`, `auth_http_403`, node HTTP errors or a deadline are incomplete diagnostic evidence; they do not establish a healthy source. The script prints fixed error codes rather than credentials or response bodies.

## Protocol limits

Keep node and limiter clocks synchronized. A consistently slow source clock is indistinguishable from old history, and a fast clock can produce rejected future timestamps. This feature does not configure NTP or alter clocks. During a daylight-saving fold, a naive local timestamp cannot identify which occurrence it means; use verified UTC process time when operationally appropriate rather than guessing a fold.

The printed timestamp is Xray's local log-emission time after its internal queue, not an independently captured connection-event timestamp. Xray/node queues can drop records without loss metadata. The stream has no sequence or replay cursor, so timestamp validation and bounded fingerprints cannot prove complete telemetry or distinguish every possible replay/live case. The limiter exposes uncertainty it can detect; it does not promise absolute replay detection or pre-queue timing accuracy.
