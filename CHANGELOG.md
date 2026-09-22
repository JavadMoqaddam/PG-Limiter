# Changelog

All notable changes to PG-Limiter are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.1] - 2026-09-22

A security-hardening release. A report-only whole-tree security review found no critical
or high-severity issues; authorization gates and query parameterization were sound. This
release fixes the confirmed medium and low findings — all defense-in-depth, with no
change to externally observable defaults — and adds an automated pull-request security
review to CI. Two behavior-changing findings (flipping the panel `PANEL_VERIFY_SSL`
default and running the container as non-root) are deferred to avoid regressions and are
listed under Known limitations.

### Added

- **Automated security review in CI.** A new workflow runs Anthropic's
  `claude-code-security-review` on every pull request into `main`, reading its API key
  from the `ANTHROPIC_API_KEY` repository secret. It complements the existing CodeQL and
  Bandit scans.

### Security

- **Panel token-endpoint error bodies were relayed verbatim to Telegram and logs.** The
  untrusted panel response body is no longer forwarded into the HTML-parse-mode operator
  channel or written unbounded to `app.log`; only the status code is reported, and the
  truncated debug body is `repr()`-escaped.
- **Panel-derived usernames and IP activity were interpolated unescaped into HTML
  messages.** Enable notifications, callback confirmations, the set-limit prompt, and the
  warning/disable/revoke notifications now HTML-escape externally sourced values before
  embedding them, so panel- or MITM-supplied markup cannot inject into the operator view.
- **Usernames were interpolated unencoded into panel API request paths.** Every
  `/api/user/{username}` path now percent-encodes the username, preventing request-path
  reshaping.
- **GeoIP lookups disabled TLS verification and interpolated IPs unencoded.** The GeoIP
  client now verifies certificates for its public CA-signed endpoints, and IP addresses
  are URL-encoded before use.
- **Secret files were written world-readable and the installer had injection-prone
  paths.** The installer now `chmod 600`s the `.env` (panel password + bot token) and the
  backup archive, replaces a `sed`-injection-prone env-var writer with a data-safe one,
  and validates the downloaded script (`-f` plus shebang and marker checks) before
  installing it.
- **The startup reachability probe hardcoded disabled TLS verification.** It now honors
  the operator's `PANEL_VERIFY_SSL` setting (default unchanged).

### Known limitations

- The panel API client still defaults `PANEL_VERIFY_SSL` to `false`; enabling it by
  default would break self-signed-panel deployments and is deferred to a dedicated change.
- The container still runs as root; adding a non-root user requires re-owning the mounted
  data volume and is deferred.

## [1.6.0] - 2026-09-19

An audit-remediation release. A full-project audit found that enforcement intent was
sound but the persistence, notification, and asynchronous-ownership boundaries around it
were not reliable under rollback, concurrency, partial failure, or operator recovery.
This release closes the P0 and P1 findings in scope and hardens the P2 boundaries, each
fix landing test-first. Three P1 items and two P2 items are deliberately out of scope
and are listed under Known limitations.

### Fixed

- **REST enable endpoints erased the only recovery record without enabling the panel
  user.** `DELETE /users/disabled/{user}` and the bulk variant cleared the SQLite
  registry (and group `original_groups`) without ever calling the panel, so the account
  stayed disabled while automatic recovery could no longer find it. Enable now goes
  through the panel first and clears only confirmed users; a panel failure preserves the
  record.
- **User-metadata cache invalidation ran before commit, and bulk deletion skipped it.**
  Between invalidation and commit an enforcement reader could re-cache the old committed
  row, leaving whitelist, monitoring, group and limit decisions stale until a full
  refresh; `delete_many()` left deleted users cached. Invalidation is now registered
  transaction-locally and published on `after_commit`, cleared on rollback, and covers
  every deleted username.
- **Unknown active users were skipped but never queued for synchronization.** An active
  username missing from batch metadata was skipped without calling
  `queue_unknown_user_fetch()`, so a new account avoided limit evaluation until the next
  full sync. It is now queued once (deduplicated) while still failing closed on defaults.
- **Cross-process JSON persistence could truncate and lose updates.** Management writes
  now serialize through a stable sidecar lock with atomic replacement and read-modify-
  write transactions; a malformed read is preserved and surfaced rather than replaced
  with environment defaults.
- **Legacy tables could resurrect cleared policy on restart, and migrations could report
  success after lossy or incomplete schema changes.** Legacy state is migrated once under
  Alembic rather than replayed at every startup, and revision 007 now merges duplicate
  `ip_history` rows (min `first_seen`, max `last_seen`, summed `connection_count`, per-
  column newest-non-null metadata) in a staged, verified, idempotent rebuild instead of
  keeping only the highest-id row.
- **The Telegram dispatcher was racy, unowned, non-idempotent, and could not drain on
  shutdown.** Critical-message persistence now snapshots under the lock, propagates save
  failures instead of queueing an unpersisted message as durable, tracks queued `db_id`s
  so a worker restart does not re-send, awaits cancel persistence, and drains the queue
  before the worker exits.
- **A notification dedup key was marked before delivery.** The key is now marked only
  from the delivery outcome — a single message when its future resolves, a split message
  only when every chunk is delivered — and the background marking task is strongly
  referenced so it cannot be garbage-collected mid-flight.
- **Conversation continuations trusted entry-time authorization and could be shadowed.**
  The catch-all text/document handlers moved to a later handler group so an active
  conversation is not consumed first, every mutating continuation re-checks admin
  privilege, and the last administrator can no longer be removed through any path.
- **Warning and punishment state could diverge after a side effect.** A violation that
  cannot be recorded now surfaces instead of advancing only the JSON mirror; a panel
  action whose record fails is reconciled on the next attempt rather than re-applied;
  concurrent same-user punishment is deduplicated; active warnings serialize from a
  detached snapshot; disable history is added only for a real disabled/revoked outcome.
- **A failed enforcement cycle destroyed the active-log batch.** The batch consumed by
  `pop_active_users_snapshot()` is now requeued on failure (metadata unavailable or
  cancellation), merged by connection identity so a newer mid-cycle event is not
  overwritten.
- **The ISP detector kept a removed token, and its client leaked on shutdown.** A
  `config_signature`/`reconfigure()` now honours an emptied token by dropping the secret
  and enabling fallback, `close()` cancels background lookups, and shutdown closes the
  detector. Token previews are no longer logged.
- **Management surfaces wrote a retired state plane and the API lifecycle was
  incomplete.** The FastAPI lifespan now runs `init_db()`/`close_db()`, the general-limit
  and whitelist REST writes route through the canonical services enforcement reads, the
  API defaults to a loopback bind, and repeated failed Basic auth is throttled.
- **Snapshots of active users were shallow.** `isp_info`, `device_info` and `group_ids`
  are deep-cloned so a report reading a snapshot is not mutated by ongoing ingestion.
- **IPv6 subnet cache keys were malformed and spelling-dependent.** Keys are derived
  through `ipaddress` (/24 for IPv4, /64 for IPv6) so equivalent spellings collapse to
  one row; unparseable input is returned unchanged.
- **Retention had no owner.** A supervised loop prunes the violation history and ISP
  subnet cache every six hours (30-day window); both cleanups previously had no live
  caller.
- **`get_db` did not roll back on cancellation.** It caught `Exception`, which excludes
  `CancelledError`; it now catches `BaseException`, rolls back on cancellation, and
  shields rollback and close so a second cancellation cannot leak a connection.
- **Settings handlers confirmed writes that never persisted.** Device-count, CDN and
  node toggles, admin-filter, group-filter and punishment handlers now persist first and
  confirm only on success.
- **The detailed-monitoring view called a method that did not exist.**
  `analyze_user_activity_patterns` is implemented from the record already held.
- **The IP-history report claimed device-limit parity it did not have.** It is reframed
  as unique IPs over the window (history, not the device count used to ban) and its ISP
  detector now reads the canonical config keys and is closed after use.
- **Top-level entry points were stale.** `limiter` no longer parses argv at import, and
  the status CLI reads the SQLite registry instead of the retired `.disable_users.json`.

### Changed

- Dependabot now tracks pip, GitHub Actions and Docker, grouping routine minor/patch
  upgrades into one PR per ecosystem.
- Dependencies refreshed to the latest compatible releases (sqlalchemy 2.0.54, alembic
  1.20.0, uvicorn 0.53.0, cachetools 7.2.0, typer 0.27.2).
- Ruff targets the CI floor (py312) and now lints the test suite for correctness rules
  (E9/F63/F7/F82) instead of ignoring it wholesale.

### Known limitations

Out of scope for this release and unchanged:

- **P1-2** — non-SQLite `DATABASE_URL`s are still accepted although runtime SQL is
  SQLite-only; the recommended near-term contract (explicit rejection) is not yet
  implemented.
- **P1-7** — backup/restore still copies live files without staging, manifest/integrity
  checks, writer quiescence or atomic swap.
- **P1-13** — node heartbeat, source-generation and cancellation ownership remain
  incomplete.
- **P2-10** — operational hardening (panel TLS-verify default, secret file modes, PID-1
  signal forwarding, immutable image IDs, release-workflow gating) is not addressed.
- **P2-11** — the broad enforcement modules are not decomposed; this awaits
  characterization tests per the audit's own guidance.

## [1.5.0] - 2026-09-03

A correctness release, like 1.4.1, but this one changes behaviour in enough places to
warrant a minor bump. The theme is settings that did not mean what they appeared to
mean: a configuration object every task shared and could edit, writes that failed
without saying so, and a filter change that took five minutes to take effect. Several
of the fixes below were reachable in log mode, not just in the newer API mode.

### Fixed

- **A device limit could resolve below 1, which bans on the first device.** Four of the
  five exits in `resolve_effective_limit` returned a stored `0` verbatim, and the widest
  of them is the general limit - the one that applies to every user with no special or
  group limit. The bot had no floor at either general-limit prompt, nor in
  `save_general_limit`, nor in `read_config`, and `GENERAL_LIMIT=0` in `.env` worked too;
  the CLI and the HTTP API already refused it. An operator typing `0` to mean "no limit"
  would have banned the entire installation after `MAX_WARNING_COUNT` scans. Now floored
  in three layers: unusable values fall through to the next limit, an unusable general
  limit uses the documented default of 2 and says so at CRITICAL, the config layer
  validates `GENERAL_LIMIT` and the database row once at load, and the bot refuses it at
  input with a message pointing at the whitelist. A 5,760-combination sweep confirms
  nothing resolves below 1.
- **`read_config()` handed out the shared cache instead of a copy.** One mutable dict
  with no TTL was returned to every caller in the process, from both exits, so a
  Telegram handler doing `config_data["disabled_nodes"].append(id)` changed what the
  enforcement loop saw for the life of the process, with nothing in the log. Eighteen
  such writes existed across five settings handlers, all on containers enforcement
  reads. Each was normally masked by the `save_config_value` that followed, but that
  function returned `False` *without* dropping the cache when the write failed, and two
  node handlers put a Telegram round-trip between the edit and the save. Both exits now
  deep-copy: 33 µs per read, 0.011% of a 180 s cycle. A node wrongly dropped from
  `disabled_nodes` gets its connections counted again, which is the direction that ends
  in a ban nobody earned.
- **A failed settings write reported success.** `save_config_value`,
  `delete_config_value` and the punishment-settings helper caught every exception and
  returned `False` with nothing logged - the punishment one used `print`, which does not
  reach the container log. Callers show a green tick without reading the return value, so
  a write that never landed looked applied and silently reverted at the next cache
  rebuild. Failures are now logged with the key and a traceback, the cache is dropped
  whether or not the write committed, and `save_ipinfo_token` propagates the real result
  instead of a hardcoded `True`.
- **A group-filter change did not apply until the next user sync.** `is_monitored` is
  pre-computed per user and read from the RAM metadata cache by the enforcement gate.
  All eight Telegram writes that change the filter updated the config and dropped the
  config cache but never recomputed the flag, so for up to one `user_sync_interval` -
  five minutes by default, fifteen if configured that way - tightening the filter kept
  enforcing exactly the users who had just been excluded. Three cycles at a 180 s
  interval fit inside that window. `recompute_all_user_limits` also now refuses a
  degraded configuration, which reads as "group filter disabled" and would have marked
  every filtered-out user monitored again.
- **API mode counted IPs whose age could not be established.** The freshness filter only
  applied to values at or above the epoch floor; anything below skipped both the
  staleness and future-timestamp checks and was counted as a live device, on the
  assumption that a small value is a legacy connection count. Since the payload parser
  coerces anything unparseable to `1`, an ISO-8601 string, a float-formatted epoch, a
  bool, a list or a null all landed there - and the panel never expires an entry, so a
  user who merely changed network looked like several simultaneous devices. It was also
  invisible: no counter moved, so no gate fired. Such values are now dropped as unknown
  age and folded into the stale count, so a wholly undatable payload skips the cycle
  instead of publishing an empty snapshot that clears every pending warning.
- **Group Limits narrowed the API-mode candidate query.** A group limit is a limit, not
  a monitoring scope: users in no limited group are still judged against the general
  limit. Restricting `GET /api/users` to the `group_limits` keys left them uncollected,
  so they were never counted, warned or banned - and coverage, measured over the
  narrowed target set, still read 100%, so no gate noticed. Exclude mode fell through
  into the same branch despite a docstring saying it did not, which meant a limit on the
  excluded group made the query ask for precisely the users enforcement must ignore.
  Only a group filter in `include` mode narrows now, and the admin filter never does -
  the enforcement gate is fail-open on ownership and the panel cannot express "…or has
  no known owner".
- **The "enforcement has stopped" alarm was unreachable in three ways.** The
  coverage-skip streak was cleared as soon as the per-user fan-out looked healthy, before
  the two gates that follow it, so an all-stale or low-node-coverage skip could repeat
  forever without the streak passing 1. The empty-candidate path cleared the streak and
  the one-shot flag as well, so an alternating pattern of empty and skipped cycles never
  accumulated. And the "every IP filtered as stale" skip - clock skew, which does not
  heal by itself - only wrote a log line. All three now feed one streak meaning "cycles
  skipped since enforcement last actually ran".
- **The cleanup pass deleted the records the new guards had just preserved.**
  `check_persistent_violations` returns without judging anything when the sample is
  partial, then `cleanup_expired_warnings()` ran unconditionally and removed exactly
  those records - so a partial sample still wiped real offenders' counters. It now takes
  the same trustworthiness verdict. The companion `known_users` guard also matched every
  *offline* monitored user rather than only unresolvable ones, logging a warning per user
  per cycle for a record that was then deleted anyway.
- **The node list cache mixed its two scopes.** It was keyed on the panel domain alone,
  but `enabled_only=True` and `False` are different result sets, so the node-status poller
  asking for the full fleet overwrote the entry that enabled-only callers then read -
  serving them disabled nodes for up to an hour, including the node-coverage denominator.
- **The second ban path had none of the main loop's guards.** `check_persistent_violations`
  could ban on a sample the main loop had already judged untrustworthy.
- **A lowered `max_warning_count` applied retroactively** to streaks already in
  progress, so a user at two of three warnings was banned by the next cycle after the
  operator lowered the threshold to two. The required count is now pinned per record at
  creation.
- **A special limit of `0` banned the user instead of exempting them.** Nothing treated
  `0` as unlimited on this path, so the user was judged against a limit of zero devices
  and banned on first sight. Values below 1 are now refused at every input with a message
  pointing at the whitelist, which is the actual exemption mechanism.
- **A malformed punishment duration was trusted as-is.** A string or `null` in the stored
  ladder reached the ban path without coercion; only an explicit `0` should mean a
  permanent disable. Twelve malformed shapes were tested against the parser.
- **Node reconnection acted on an hour-old node list.** The poller read the cached list,
  so a node that had reconnected minutes ago kept being treated as down for up to an
  hour, and disabled nodes were omitted from the list entirely rather than being seen and
  skipped.
- **The panel-user parser was given local state the panel does not have.** `is_excepted`
  was hardcoded, so every whitelisted user was upserted as monitored, and the
  single-user sync path did not pass the config at all. All three derived inputs are now
  required arguments so the defect cannot recur silently.
- **Backup restore wrote keys `read_config` does not read.** Settings were restored into
  `timing`/`display`/`punishment`/`group_filter` blobs while the reader looks at flat
  keys, so a restore appeared to succeed and changed nothing. The panel password was also
  being written to the backup archive for no consumer; that write is gone.
- **The enhanced-details toggle did nothing.** The reporting path read
  `config["display"]["show_enhanced_details"]`, a path `read_config` never creates, so it
  always fell back to on.
- **The SSE connect phase had no timeout** and ignored `PANEL_VERIFY_SSL`, so a node
  whose TCP connect hung held its task open indefinitely.

### Added

- **`API_IP_MIN_COVERAGE`** in `.env`, as a percentage (default 80). This is the share of
  queried users that must answer before an API-mode cycle is allowed to enforce; below
  it the cycle is skipped rather than acting on a thin sample, because a thin sample
  clears the violation counters of real offenders. Both spellings are accepted (`80` and
  `0.8`), and a malformed value falls back to the default rather than to zero - a floor
  of zero would silently disable the guard.
- **`API_IP_MIN_NODE_COVERAGE`** in `.env`, also a percentage, off by default. The user
  coverage gate is an answer rate per user and reads 100% even when two thirds of the
  fleet is silent, which under-counts devices. The key existed and was read but had no
  writer anywhere, so the gate was unreachable - and it was parsed with a plain
  `float()`, so a hand-written `80` became 80, clamped to 100%, and would have skipped
  every cycle.
- **The "Last API Cycle" screen shows node coverage with its denominator** ("1/49 (2%)"
  rather than a bare "Nodes seen: 1"), and lists undatable and future-dated IP drops next
  to stale ones. Both numbers were already recorded and never rendered.

### Changed

- **The Docker image is pinned to the release version** instead of `:latest`. A mutable
  tag meant `docker pull` fetched whatever was last built from `main`, the image being
  replaced lost its tag so there was no simple way back, and two servers could run
  different code while both reporting `latest`. Updating now requires the matching
  `v*` git tag to have been pushed, and says so plainly when it has not.
- **Every staleness window is now three check intervals**, with a 120 s floor, instead of
  a mix of two intervals and hard-coded values. One helper, `node_silence_window`, is the
  single source: node-status polling, the half-open stream detector and the
  sample-trustworthiness check all read it, so they cannot drift apart. At the default
  180 s interval that is 540 s.
- **A live SSE stream is reconnected when the panel calls the node connected but the
  stream has produced nothing** for one silence window. Keep-alive lines count, so
  silence means a dead stream rather than an idle node.

### Removed

- **The unreachable settings screens.** `settings_intervals.py` in full, plus the
  country-filter and single-IP handlers in `settings_display.py`: complete, correct
  code that no command, callback route, or keyboard button ever reached. Both values
  are configured through the environment (`CHECK_INTERVAL`, `TIME_TO_ACTIVE_USERS`,
  `COUNTRY_CODE`). Every name was grep-verified to have no remaining referent before
  removal.
- Unused imports across `utils/`, `db/`, `cli/`, `api/` and the root modules. `ruff.toml`
  has ignored `F401` globally with the note "too many to fix now", so these had been
  accumulating unchecked.

## [1.4.1] - 2026-08-31

A correctness release. Every entry below is a bug that was reachable in 1.4.0,
and several were observed on a live installation rather than inferred from code.

### Fixed

- **Migrations silently never applied on an upgraded install.** `start.sh` stamped an
  unversioned database at `head`, which declares every revision applied without
  running any of them. The corrective revisions therefore never executed on the one
  kind of database that needed them, and the only symptom was `ON CONFLICT clause
  does not match any PRIMARY KEY or UNIQUE constraint` once per cycle - so IP history
  stayed empty forever. It now stamps `006_drop_patterns` and upgrades from there,
  and it also detects and repairs a database a previous version already mis-stamped.
  Every revision after 006 is idempotent for this reason. (`1e33fec`, `c68fc62`)
- **A partially dead node fleet went unnoticed.** Log streams are opened with no read
  timeout, so a half-open connection never raises and the node keeps reporting
  "Connected" while delivering nothing. Users on that node looked disconnected, and
  their consecutive-violation counters were cleared every cycle - a real offender
  never reached the third scan. A per-node heartbeat now distinguishes a silent
  stream from a genuinely idle node. (`6dea0a0`)
- **An unset API password authenticated every caller.** The "no password configured"
  branch only logged a warning, so `compare_digest(b"", b"")` succeeded and the
  default username with an empty password opened every route - including the ones
  that enable users and rewrite config - on a server whose default bind address was
  `0.0.0.0`. Requests are now refused. Docs endpoints are off by default and CORS is
  no longer a credentialed wildcard. (`68f215b`)
- **Imported violations were stamped with the current time.** Old violations counted as
  "just now", which jumped affected users to the harshest punishment step. Original
  timestamps are preserved and the import is skipped when the table already has rows.
  (`438c681`)
- **Part of the bot token was printed at boot.** `${BOT_TOKEN:0:15}` includes the first
  characters of the secret half. Only the public id half is logged now. (`ce5ac83`)
- **One failing user ended the whole enforcement cycle**, leaving every remaining user
  unevaluated. Each user is now isolated; a failure leaves that user's counter
  untouched and the cycle continues. (`b6e485c`)
- **The admin was shown violation counts from a stale JSON copy** while punishment was
  decided from SQLite, so "Next Punishment" could disagree with what was applied. The
  violation-history command also raised `KeyError: 'time_ago'`. (`f308dce`)
- **API IP mode mis-measured coverage.** The guard compared answered users, never node
  coverage, so a third of the fleet could be silent while coverage read 100%. Node
  coverage is now measured and logged, repeated coverage skips raise one alarm, and a
  coverage skip no longer resets the dead-cycle counter that triggers the automatic
  fallback to log mode. (`6131aab`, `3fcbaac`, `d1706b6`)
- **The legacy JSON import marked itself done before succeeding**, so a transient failure
  skipped it permanently. (`1520c6b`)
- **Device counting fell back to a raw IP count** for users with no connection detail,
  ignoring subnet grouping - the same metric that previously produced false
  positives. (`6c50624`)
- **The "Enhanced Details" button silently enabled the setting** instead of opening its
  submenu. (`0fc1aca`)

### Added

- Seven registered Telegram commands that help never mentioned are now documented:
  `/punishment_set_steps`, `/group_filter_add`, `/group_filter_remove`,
  `/admin_filter_add`, `/admin_filter_remove`, `/users_by_node`,
  `/users_by_protocol`. (`a20e582`)
- `tests/test_node_heartbeat.py` and expanded device-counting and API-IP tests.

### Changed

- The installer's `update` path takes a database backup with the service stopped,
  tags the outgoing image for rollback, regenerates the compose file, and verifies
  the container actually started before reporting success. Redis is no longer
  declared, so it is removed on the next update.

## [1.4.0] - 2026-08-30

A large refactor. Redis is gone, SQLite is the only store, the schema is managed by
Alembic, and the IP source is switchable. None of that is a patch-level change.

### Removed

- **Redis.** Every cache is now in-process, one per data type, and SQLite is the only
  durable store for bans, limits, warnings and violation history. (`6a52013`)

### Changed

- **Schema is under Alembic.** An existing database created by `create_all` is detected
  and stamped rather than failing on `001_initial`, and a migration failure is now
  fatal at boot instead of a warning - a schema the code does not expect is worse
  than a container that will not start. (`cc1d202`, `752cb19`)
- **The IP source is switchable** between node log streaming (SSE) and the panel API,
  with a shared admission rule and a single bounded cache so the two modes cannot
  disagree about whether an address counts. (`e98854c`, `d0c3ed0`, `b833c60`)
- **Device counting moved into its own tested module** instead of living inside the
  enforcement loop. (`21eb44f`, `2c5e1fd`, `b4289b2`)
- **One process per lifetime.** The limiter no longer restarts its own event loop in
  place; a crash exits non-zero and the supervisor starts a fresh interpreter. The
  old behaviour left module globals bound to a dead loop, which is how the bot could
  go mute while enforcement kept banning. (`05d3b97`)
- **Disabled users live in one registry** instead of five parallel copies. (`425dbdf`,
  `c4077c6`)
- **Telegram callbacks route through a table** instead of a 900-line if-chain, and the
  2300-line settings handler is split into one module per domain. (`7f492dd`,
  `ee5b550`)

### Fixed

- **An unknown limit no longer falls back to 2.** A transient database error used to
  produce a partial metadata read, which made every active user look monitored with a
  default limit of 2 - banning users whose real limit was higher. A degraded read now
  skips the whole cycle: nobody is warned, banned, or cleared while limits are
  unknown. (`e735833`)
- **An empty sample no longer clears everyone's counters.** No active users while users
  are under monitoring means the pipeline stopped, not that everyone disconnected.
  (`fb7be98`)
- **A failed cycle no longer restarts the process**, which used to compress the ~9-minute
  escalation window into ~1-2 minutes. A time guard now also prevents the
  consecutive-violation counter from advancing faster than the scan interval.
  (`b55e771`)
- **`ip_history` got the unique index its upsert always relied on**, plus seven indexes
  that existed only in `models.py` and were therefore absent from every upgraded
  install. (`cc57ee8`, `17c307f`)
- **CDN keys no longer multiply one IP into several devices.** (`ae30c7d`)
- **API IP freshness is judged per user fetch time**, so a slow fan-out cannot mark fresh
  addresses stale, and an all-stale cycle is skipped rather than published. (`c23ead3`)

### Added

- CI runs migrations against a populated database, checks the migrated schema against
  `models.py`, executes the real upsert, imports every module, and blocks on
  undefined names. The previous migration job pointed its two halves at two different
  database files, which is why the defects above went unnoticed. (`4d7cf5c`)

[1.4.1]: https://github.com/JavadMoqaddam/PG-Limiter/releases/tag/v1.4.1
[1.4.0]: https://github.com/JavadMoqaddam/PG-Limiter/releases/tag/v1.4.0
