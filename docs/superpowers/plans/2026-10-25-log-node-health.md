# Logs node isolation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the
> independently owned tasks below. Record failing regression tests before code.

**Goal:** Continue safe monitoring on healthy included nodes and expose degraded
coverage through Telegram General.

**Architecture:** Freeze per-node readiness and epochs into Logs evidence. Permit
partial positive decisions only in monotone modes and preserve unknown warning
state. A separate transition reporter uses the existing outbound dispatcher.

**Tech Stack:** Python 3.12–3.14, asyncio, pytest, existing Telegram dispatcher.

**Spec:** ../specs/2026-10-25-log-node-health-design.md

## Global constraints

- Per Node is intentional; relay forwarding stays unchanged.
- Global source/settings changes and unknown/stale/malformed fleet block actions.
- Partial absence or below-limit evidence cannot clear warnings.
- Recovered nodes require one full check interval and cannot enter old snapshots.
- Preserve authentication, HTML escaping and monitoring admission guards.
- No real panel/Telegram calls in tests; no private logs or credentials in commits.
- Keep replay cache default at 10000; no new dependencies or migrations.

## Task 1: Per-node evidence

Files: utils/log_observations.py, utils/shared_state.py, utils/parse_logs.py;
tests/test_log_node_isolation.py and affected Logs tests.

Interface: LogSampleEvidence adds required_node_ids, node_versions and
coverage_complete. eligible_node_ids contains only captured ready nodes.
LogObservations.is_current(evidence, *, allow_partial=False) preserves strict
default behavior. node_epoch(node_id) protects parser commits.
retain_log_interval uses snapshot evidence when attached.

- [ ] Write regression tests for one unavailable node with other nodes ready,
  frozen eligible IDs, reconnect warmup, own-node parser retirement and faults.
- [ ] Run focused pytest and confirm failure from global invalidation.
- [ ] Implement per-node epochs/floors/faults, freeze and filter evidence.
- [ ] Run existing replay, expiry, scope, parser and SSE tests; resolve regressions.
- [ ] Review node evidence independently before the final branch review.

## Task 2: Partial enforcement

Files: utils/check_usage.py, tests/test_log_partial_enforcement.py.
Consumes is_current(..., allow_partial=True), coverage_complete and ready IDs.

- [ ] Write failing tests for healthy positive Per Node/Inbound actions with one
  bad node, unknown absence/below-limit/expired preservation and limit changes.
- [ ] Verify failures before implementing the explicit partial-mode admission.
- [ ] Protect policy users whose healthy count is absent or <= current limit;
  recheck after policy awaits. Keep other counting modes strict.
- [ ] Exercise persistent, ordinary, cleanup and final punishment paths and
  completed-scan reporting, including fault/source/pause changes during awaits.
- [ ] Review enforcement independently and run related warning/policy tests.

## Task 3: General health transitions

Files: utils/node_health_notifications.py, utils/get_logs.py and/or existing
cycle/status hooks, tests/test_node_health_notifications.py.

- [ ] Write failing tests for disconnect/gap, recovery, coverage transitions,
  deduplication, exclusion, pause/API suppression and escaped node names.
- [ ] Implement a nonblocking reporter using existing dispatcher guards. Observe
  state from authoritative fleet/transport checks; avoid per-record writes.
- [ ] Verify routing to General and transitions through mocked dispatch.
- [ ] Review reporter and hooks independently.

## Task 4: Profile-backed SSE hot path

Files: utils/parse_logs.py, utils/log_observations.py,
tests/test_log_parser_hotpath.py; synthetic benchmark if needed.

- [ ] Record bounded-work regression failures and synthetic baseline timings.
- [ ] Cache timestamp conversion by verified zone and printed second with a
  fixed memory bound; check future source time on each receipt.
- [ ] Reuse unchanged authoritative parser configuration, with explicit mutable
  configuration, revision changes and invalidation retaining their guards.
- [ ] Reuse record regex matches; preserve filtering, numeric IDs, replay
  identity and atomic post-await admission.
- [ ] Compare synthetic timings and run the full Logs regression group.

## Task 5: Verification and draft PR

- [ ] Run CI-equivalent pytest, local import checker, blocking Ruff and changed
  Python formatting; inspect the complete branch diff against origin/main.
- [ ] Review all existing runtime changes plus these additions independently.
- [ ] Update installation/runtime docs with coverage and health behavior.
- [ ] Commit on the existing isolated branch, push and create the authorized
  draft PR; do not merge or deploy.

Production profiling supplied evidence for Task 4. Sampling errors limit exact
attribution; local speedups do not establish a production CPU percentage.
