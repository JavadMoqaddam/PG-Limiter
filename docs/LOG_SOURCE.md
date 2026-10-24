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

Both timezone settings are unset by default. A valid per-node override wins over the optional default. An invalid override cannot inherit that default. Use exact IANA zone names supported by the limiter's system timezone database, such as `Asia/Tehran`, `Europe/Berlin`, or `UTC`. Invalid JSON, an invalid capacity, or a missing/invalid required-node zone leaves enforcement waiting. Environment changes require restarting the limiter; this does not change any host or container clock. Panel API collection is independent of these settings.

The VPS host timezone selected by `timedatectl` does not establish a container's timezone. The official node container setup does not automatically mount the host's `/etc/localtime` or forward its timezone. Verify the running Xray container/process environment, timezone files and effective time before configuring it. Do not infer a timezone from the VPS country, node IP, panel display, limiter host, or a similarly named container on another host. Several checked UTC containers establish only those containers' state, not the fleet's timezone.

## What counts

The limiter preserves the original printed timestamp, converts it to UTC and counts accepted records within the completed `CHECK_INTERVAL` window. It supports `YYYY/MM/DD HH:MM:SS`, with optional one-to-six fractional digits; current Xray prints six. Different records with the same timestamp remain distinct. Both `>>` and `->` inbound separators remain supported. Receipt time never makes old history fresh.

Full-record SHA-256 fingerprints are scoped per node and retained across stream reconnects, consumed snapshots and periodic task rebuilds. Repeated history cannot create a fresh observation or increment a connection count. Pending observations survive reconnects and expire by their original timestamps. Changes to source, check interval, timezone settings or cache capacity retire the previous generation and require a complete ordinary check interval before enforcement resumes. They do not reset warning history or change the configured warning threshold, punishment, device counting, or probe policy.

A snapshot retains its completed interval through slow ISP and metadata preparation. Changes to configuration or evidence during preparation invalidate the sample; an old parser or failed preparation cannot write its old-generation observations into a newer source sample.

## When enforcement waits

The existing IP Source settings and node status messages show Logs readiness. An unknown/stale fleet list, a required node without a valid source zone, a non-disabled node disconnected from the panel, or missing/stale log transport makes the cycle unavailable. A missing fleet is not treated as an empty sample. SSE comment pings update transport liveness even when no accepted record arrives; panel pings establish panel-to-limiter transport only, so panel node status is checked separately. After a known transport gap or stream reconnect, a complete ordinary check interval must be observed again; the first HTTP response or recovered heartbeat cannot certify the missing preceding interval. Pending observations and replay fingerprints survive this warmup.

An otherwise usable accepted record with a malformed, future, ambiguous daylight-saving, or nonexistent daylight-saving timestamp makes the affected interval unavailable. Known old history is harmless and is ignored. Unavailable cycles preserve warnings: they cannot issue a warning, ban, clear a record or clean up expired warnings. A complete valid empty or expired interval can clear records.

Replay memory is bounded at **10000 records per node** by default. If capacity evicts a fingerprint that could still qualify in the current window, enforcement waits until that record's original timestamp is outside the window. Busy nodes may need a larger `LOG_REPLAY_CACHE_SIZE`; this is a memory/availability tradeoff, not a silent replay allowance. After automatic API fallback, Logs uses the same readiness checks and warmup, so unset zones keep enforcement waiting rather than guessing.

## Protocol limits

Keep node and limiter clocks synchronized. A consistently slow source clock is indistinguishable from old history, and a fast clock can produce rejected future timestamps. This feature does not configure NTP or alter clocks. During a daylight-saving fold, a naive local timestamp cannot identify which occurrence it means; use verified UTC process time when operationally appropriate rather than guessing a fold.

The printed timestamp is Xray's local log-emission time after its internal queue, not an independently captured connection-event timestamp. Xray/node queues can drop records without loss metadata. The stream has no sequence or replay cursor, so timestamp validation and bounded fingerprints cannot prove complete telemetry or distinguish every possible replay/live case. The limiter exposes uncertainty it can detect; it does not promise absolute replay detection or pre-queue timing accuracy.
