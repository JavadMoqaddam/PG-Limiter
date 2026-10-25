# Logs node isolation and health reporting

An unavailable included node currently invalidates every Logs sample. Preserve
monitoring of independently healthy nodes while keeping warning decisions tied
to authoritative, immutable evidence.

Keep the global source/settings epoch, authoritative fleet freshness and global
uncertainty checks. Track node status, transport floor, epoch and uncertainty
separately. Capture required IDs, ready IDs, node versions and coverage in each
sample. Filter connections to the captured ready IDs and reject records prepared
before their own node epoch changed.

Per Node and Per Inbound are monotone set counts: healthy observations can prove
an over-limit violation even with incomplete coverage. Incomplete evidence must
preserve warnings for absent or below-limit users, including expired records.
Recheck positive evidence after each current-policy refresh. Device/IP modes with
CDN overlap continue requiring complete coverage. Recovered nodes need a full
check interval; they cannot enter an older snapshot.

Use the existing Telegram dispatcher for General-topic health events. Report
included-node panel disconnects, missing/stale SSE transport, uncertainty,
recovery and partial/blocked monitoring coverage on state transitions. Excluded
nodes and intentionally paused/API monitoring must not generate SSE incidents.
Escape names; include node IDs, safe reasons and coverage, never access records,
usernames, addresses or credentials. Repeated polls of the same state produce no
new notification. Delivery remains subject to existing destination/admission
guards and does not block ingestion.

No database migration, replay-cache default change or new dependency is needed.
The CPU trial identifies repeated parser/configuration/timestamp work in the SSE
path. Reuse unchanged authoritative parser configuration and bounded timestamp
conversion by printed second, while preserving per-record future-time validation,
source changes, mutable explicit configuration and per-node commit guards. Reuse
record regex matches without changing accepted-record policy. Benchmark synthetic
input locally and retain correctness regression tests. Production CPU improvement
requires a follow-up profile. Defer panel-sync performance optimization.
