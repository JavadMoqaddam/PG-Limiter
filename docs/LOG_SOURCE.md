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

If every monitored Xray process has been verified to use UTC, set `LOG_TIMEZONE=UTC` in the limiter's `.env`. With the standard installer this is `/etc/opt/pg-limiter/.env`; run `sudo pg-limiter config`, add the setting, then run `sudo pg-limiter restart` to recreate the container with the updated environment. With a manual Compose installation, edit its `.env` and run `docker compose up -d --force-recreate pg-limiter`. A plain `docker restart` retains the old container environment. Per-node overrides still take precedence, so correct any outdated `LOG_NODE_TIMEZONES` entries too. After recreation, allow a full `CHECK_INTERVAL` with healthy fleet/log transport before expecting enforcement.

## What counts

The limiter preserves the original printed timestamp, converts it to UTC and counts accepted records within the completed `CHECK_INTERVAL` window. It supports `YYYY/MM/DD HH:MM:SS`, with optional one-to-six fractional digits; current Xray prints six. Different records with the same timestamp remain distinct. Both `>>` and `->` inbound separators remain supported. Receipt time never makes old history fresh.

Full-record SHA-256 fingerprints are scoped per node and retained across stream reconnects, consumed snapshots and periodic task rebuilds. Repeated history cannot create a fresh observation or increment a connection count. Pending observations expire by their original timestamps. Changes to source, check interval, timezone settings, cache capacity or limiter disabled-node settings retire the previous generation and require a complete ordinary check interval before enforcement resumes. A panel node's disconnect, reconnect or transport gap retires that node's evidence independently; a new or recovered node needs its own complete interval. These transitions do not reset warning history or change the configured warning threshold, punishment, device counting, or probe policy.

A snapshot retains its completed interval, required node IDs and independently ready node IDs through slow ISP and metadata preparation. Only captured ready nodes contribute observations. Configuration changes or faults affecting a captured node invalidate the sample; a recovered node cannot enter an older snapshot. A parser prepared before its own node epoch changed cannot write observations after recovery.

## When enforcement waits

The IP Source settings and node status messages show Logs readiness. Nodes in the limiter's `disabled_nodes` list and panel nodes with status `disabled` or `limited` are excluded: they require no stream, timezone, timestamp or transport evidence, and their observations do not contribute to counting. An unknown, malformed or stale authoritative fleet blocks enforcement. An authoritative empty monitoring scope becomes valid empty evidence after the complete-interval warmup; a missing fleet is never treated as empty.

An included node without a valid zone, panel connection, live transport or full interval is unavailable independently of healthy nodes. **Per Node and Per Inbound** can continue positive enforcement when healthy observations alone exceed the current user limit. Partial coverage cannot clear warnings for absent or below-limit users, including expired warnings. Per Device and Per IP continue requiring complete coverage because their CDN/grouping rules do not guarantee a lower bound from missing observations. Complete valid empty evidence can clear records normally.

SSE comment pings update transport liveness even without accepted traffic; they establish panel-to-limiter transport only, so panel node status is checked separately. After a known gap or reconnect, that node must observe a complete ordinary interval again. The first HTTP response or recovered heartbeat cannot certify the missing preceding interval. Replay fingerprints survive this warmup.

An otherwise usable accepted record with a malformed, future, ambiguous daylight-saving, or nonexistent daylight-saving timestamp makes the affected node's interval unavailable. Known old history is harmless and is ignored. If no valid positive partial evidence is available, the cycle preserves warnings without warning, punishment or cleanup decisions.

Replay memory is bounded at **10000 records per node** by default. A source-time expiry index reclaims expired fingerprints even when timestamps arrive out of order. Its auxiliary entries are bounded to twice the retained fingerprint count; admission and expiry use heap operations, with occasional amortized rebuilding after live evictions. If capacity evicts a fingerprint that could still qualify in the current window, that node waits until the record's original timestamp is outside the window. Busy nodes may need a larger `LOG_REPLAY_CACHE_SIZE`; this is a memory/availability tradeoff, not a silent replay allowance. Automatic API fallback uses the same Logs readiness checks and warmup.

## General-topic health events

General retains the editable node connection summary and adds incident history:
included-node disconnects, missing/stale log transport, uncertain observations,
recovery in progress and restored full-interval readiness. Monitoring coverage
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

`probe_format_version=2` also measures buffered records before the stream started:

- `oldest_source_epoch`, `newest_source_epoch`, source event span and receipt ages include all successfully parsed potential access timestamps, including stale and future records. A negative age indicates a timestamp ahead of receipt. When no timestamp parses, ages are null. `newest_record_age_at_sample_end_seconds` uses the stream's finish time.
- `recent_history_records` counts raw receipts with a nonfuture timestamp less than one interval old but before this probe's start; it includes duplicates. `recent_distinct_records` fingerprints all records that are recent at receipt, including this history. `duplicate_records` therefore includes both recent history and post-start records. `sample_cache_scope` labels this independent cache explicitly.
- `live_distinct_records` and its rate/projection still require timestamps at or after probe start. A slow clock or delayed delivery can leave this count at zero despite records continuing to arrive. Buffered-history counts must not be multiplied by the interval as if they were a steady live rate.
- `sse_data_records`, `records_by_receipt_second` and `record_receive_span_seconds` cover all decoded, size-bounded SSE data records, including lines without potential access records. The 16 one-second buckets are bounded; the last bucket includes receipts from second 15 onward, and `receipt_bin_overflow_records` counts receipts from second 16 onward.

Probe fingerprints remain bounded by the smaller of configured capacity and 100000 entries. A probe eviction makes `sample_deduplication_complete=false`; repeated identities can then be counted again. The probe's arrival-ordered cache can also retain expired entries behind a live entry, so its occupancy/evictions are conservative diagnostics, not proof of live pressure in the daemon. Startup tails, clock lag, delivery lag and continuing activity require interpretation of ages, receipt progression and a controlled new connection together.

To focus on panel node IDs or extend each short sample, use `python - --nodes 24,32 --duration 15` in the same command. `configuration_unavailable`, `auth_http_403`, node HTTP errors or a deadline are incomplete diagnostic evidence; they do not establish a healthy source. The script prints fixed error codes rather than credentials or response bodies.

## Protocol limits

Keep node and limiter clocks synchronized. A consistently slow source clock is indistinguishable from old history, and a fast clock can produce rejected future timestamps. This feature does not configure NTP or alter clocks. During a daylight-saving fold, a naive local timestamp cannot identify which occurrence it means; use verified UTC process time when operationally appropriate rather than guessing a fold.

The printed timestamp is Xray's local log-emission time after its internal queue, not an independently captured connection-event timestamp. Xray/node queues can drop records without loss metadata. The stream has no sequence or replay cursor, so timestamp validation and bounded fingerprints cannot prove complete telemetry or distinguish every possible replay/live case. The limiter exposes uncertainty it can detect; it does not promise absolute replay detection or pre-queue timing accuracy.
