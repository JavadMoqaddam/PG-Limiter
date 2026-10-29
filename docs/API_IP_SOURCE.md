# Panel API IP source

Select **Settings → IP Source → Panel API** in Telegram. Configure `PANEL_API_KEY`
with `nodes.stats`, `nodes.read`, `users.read`, `groups.read` and `cores.read` permissions, covering the users
you intend to monitor. Requests send `X-Api-Key`; username/password panel login
is unsupported. Grant `nodes.logs` as well if using automatic fallback to Logs.
See [Panel API-key authentication](PANEL_API_KEY.md) for setup, scopes and the
permissions needed to enforce limits. Collection queries active users with `online=true`
and the configured **Group Filter**. All Include group IDs are sent together;
an empty Include filter selects nobody. Group Limits set limits, not the query
scope. Exclusions, administrator filters and exceptions still apply.

**Last seen** is a positive whole number of seconds, T, stored in the existing
`api_ip_freshness` setting (defaulted by `API_IP_FRESHNESS`). It stays independent
of the check interval. API requests and SSE events provide different evidence:

- For Xray cores reporting version **26.4.13 or later**, an IP in the current
  response represents an open tracked connection. Its timestamp records the
  last connection opening, so an old timestamp does not exclude an open
  connection. The original timestamp is retained for diagnostics.
- Native WireGuard returns endpoint activity history, which can outlive a
  connection. WireGuard and older/unknown Xray versions retain timestamp-age
  filtering: an IP must be less than T seconds old at the start of **that
  user's actual request**. Timestamps arising during the request are allowed;
  timestamps beyond receipt time and invalid timestamps remain unknown.
- SSE/Logs continues filtering original event timestamps at evaluation. Log
  receipt, heartbeats and replay do not renew an event.

API request times are recorded after acquiring a concurrency slot. A validated
per-user result is frozen for that one collection cycle; waiting for other
users, IP preparation or warning persistence cannot turn it into an empty
observation. No earlier cycle's IPs are merged into it. Reports and warning
counts describe each user's request, not a simultaneous fleet-wide snapshot.

Before a panel punishment, the request observation must still be younger than
T. An older observation triggers a fresh lookup for **only that user**, followed
by current node/group/core and user-membership checks. A changed scope or invalid
response prevents punishment and preserves the warning. The refreshed response
uses the same counting rules even when node maps are omitted. A fresh within-limit
response prevents the pending punishment. Repeated guards
reuse a still-fresh observation; the final effect boundary checks age again.
Age is bounded from request start using both wall and monotonic clocks, so a
slow response does not get a new lifetime merely by arriving late.

For example, with T=20, a connection present in a supported Xray response still
counts when the rest of collection takes five minutes. It requires a new
observation before punishment. For WireGuard/legacy timestamps, age 19 at request
start qualifies and age 20 expires; later batch time does not change that result.
These observations do not establish ongoing byte traffic or physical devices.
The panel's `online=true` two-minute window only selects candidates; it does not
replace either source's evidence checks.

`CHECK_INTERVAL` controls collection frequency and warning timing independently of T. It does not
add a 30-second allowance or widen the candidate query. `MAX_WARNING_COUNT`,
the warning/punishment policy, /24 grouping and existing device counting rules
continue to apply. The API does not provide inbound information; use Logs when
you need inbound-specific observations.

Per IP counts distinct accepted source IPs across nodes and inbounds. The same
IP on two fresh, valid nodes counts as one in Per IP and two in Per Node. Two
accepted IPs count as two unless the configured subnet or CDN-node grouping
combines them; High Trust inbound grouping does not combine Per IP addresses.
Registered panel-node addresses are filtered from Per IP counting. An external
relay address can still be source evidence, so IPs shown in the UI and the Per IP
count do not establish the number of physical client devices.

## Incomplete evidence

A failed request, malformed body, null node map or invalid timestamp
protects the affected user for that cycle: no warning, ban
or normalization decision is made, and existing warning counters remain.
Node-ID aliases that normalize to the same ID, such as `1` and `01`, or IP keys
that become identical after trimming whitespace make the whole user's payload
malformed. They cannot overwrite conflicting evidence and clear a warning.
A successful, valid response is counted from its returned IPs. There is no
requirement for every node available to a user to appear in the response, and no
per-user missing-node comparison. A successful `{"nodes": {}}` response counts
as zero. Counts above the user's limit follow normal warning/punishment rules;
counts within the limit follow normal warning-clearing rules. Omitted maps do
not preserve warnings or trigger automatic fallback.

This accepts the panel's sparse result for enforcement: a node without a user
counter and a node query omitted by the panel have the same effect on counting.
Ordinary IP, subnet, CDN and High Trust counting rules apply without a separate
partial-response counting mode. Failed requests, returned null maps, malformed
bodies and invalid timestamps remain unknown. An 80% HTTP answer rate does not
make the failed lookups known.

Eligible node scope is resolved for each user from their enabled PasarGuard groups,
those groups' inbound tags, and the effective core configuration of each included
node. Include is the limiter's global monitoring selection; it does not grant
every user access to every included node. Multiple groups contribute the union
of their nodes. Node names such as `pm-` and the keys returned by the IP endpoint
are not used to infer authorization.

Returned node maps are filtered to the user's applicable connected-node scope.
An `error`/`connecting` node does not stop users being checked on healthy nodes,
including users in the same group. This counts the healthy monitored scope;
it does not prove that the excluded node has no live client connections.
Omitted maps add no connections; null maps invalidate that user. If
none of a user's applicable nodes are healthy, their evidence remains unknown.

Group memberships arrive with the existing online-user list. Group/core metadata
is shared across users, and each group combination shares its node calculation.
Nodes, groups and cores are read before collection and confirmed after IP
preparation; current active memberships are fetched in batches of at most 200
scanned user IDs, without an online predicate. Going offline during collection
does not itself invalidate the observed IPs. An ignored ID filter fails the check
instead of fetching the whole population. Observed membership or node-scope
changes retire only the affected users. Failed topology/permission requests
cannot become empty scopes; check `groups.read` and `cores.read` permissions.

A valid empty response (or expired activity timestamps on WireGuard/legacy cores)
can clear warnings even when some or all node maps are omitted. During a fleet outage or
topology change, retained warning users absent from the candidate list are
protected as well. A complete empty candidate list can clear warnings only when
the monitored fleet/topology remains available and unchanged through the query.
Failure to obtain the candidate list, fleet or required topology skips the cycle.
Fleet node-coverage percentages are diagnostic; the legacy
`api_ip_min_node_coverage` value does not veto usable per-user evidence.

API batches are never merged with earlier cycles or Logs, and a settings change
invalidates an in-flight batch without resetting warnings. **Last API Cycle** shows whether the sample
was complete, partially usable, or skipped.

## Reading Last API Cycle

**Users with unusable evidence** counts failed, invalid or retired user
observations. Omitted node maps do not add to this count. The per-user
`missing_node_users` and `missing_node_ids` counters have been removed.
**Nodes reporting across responses** counts eligible node IDs with valid maps
present anywhere in the batch; **Nodes absent across all responses** is the
eligible fleet minus that union. These fleet totals are diagnostic only and do
not determine whether a user response is complete or may be evaluated.

When an HTTP failure or user-coverage gate returns before node-map evaluation,
the report retains the known expected fleet and unknown-user count. With
`node_evidence_evaluated=false`, reporting nodes and global absence are unknown;
zero diagnostic counts do not establish that every node is absent.

The report retains unknown users, stale observations, elapsed collection time
and `consecutive_unsuccessful_cycles` as separate measures. A partial collection
can still enforce observed violations and resets the unsuccessful-cycle streak.
The existing three-skip coverage alert counter is separate from the two-cycle
fallback counter. Activity-timestamp filtering remains independent of scan duration.
Slow fanout does not expire
earlier per-user scan results; old observations are refreshed before punishment. Elapsed time includes collector
work even when the cycle returns all-user-unknown; a skipped cycle is not an
instantaneous collection.
`fallback_applied` records a committed automatic source switch. After Logs is
selected, Last API Cycle is labeled as historical. Pending, failed, superseded
or discarded fallback samples are not reported as available for enforcement.

## Automatic fallback and recovery

With `api_ip_auto_fallback` enabled (the default), two consecutive unsuccessful
API collection cycles switch the source to Logs. Each cycle counts once,
regardless of the number of failed user requests. Only cycles that cannot supply
an enforceable sample count: unreachable panels, incomplete candidate/topology
queries, insufficient HTTP coverage or all-user-unknown evidence. A usable partial
cycle evaluates observed violations and does not trigger global fallback.

The panel can omit failed, None or user-not-found node results. Omitted node maps
alone no longer trigger fallback, even when every response is sparse. Returned
null maps, malformed data and invalid timestamps can still make all user evidence
unusable despite successful HTTP transport and trigger fallback.
A later background mirror-pagination failure is a separate operation and does
not explain that API cycle's missing-node result.

A collection with usable evidence resets the failure streak. Valid empty results, all-valid
expired activity timestamps and confirmed deleted users count as success. Only the
panel's explicit `{"detail": "User not found"}` response confirms deletion;
a generic route or proxy HTTP 404 remains unknown evidence. Cancellation,
degraded local configuration and superseded settings do not count as outages.

Logs must be saved successfully before the switch takes effect. A failed save
keeps API selected and retries on the next unsuccessful cycle. A manual source
or settings change starts a new failure streak and prevents an older collection
from overwriting that choice. Successful source writes discard previous-source
observations, including pending API batches, while preserving warning history.
The second failed cycle cannot enforce its old API sample after switching.

A committed fallback attempts one Telegram notification. Delivery failure does
not undo or repeat the switch. Logs remains selected until you choose Panel API
again in Settings; that manual choice runs the existing permission preflight.
When automatic fallback is disabled, API remains selected and the existing
coverage diagnostics continue to report skipped enforcement.

## Configuration and upgrades

Builds containing API-key authentication require `PANEL_API_KEY` instead of
`PANEL_USERNAME`/`PANEL_PASSWORD`; changing the process environment requires a
restart or container recreation. This credential change preserves the
scope, HTTP-coverage and timestamp checks described above. Accepting sparse node
maps requires no new setting or schema migration.

`API_IP_FRESHNESS=60` supplies the default for new installations. Set any positive
whole number of seconds. The stored `api_ip_freshness` database setting takes
precedence over ENV; Telegram edits that stored setting. Invalid configuration
values are logged and fall back to the valid ENV value or the 60-second default.

The same saved threshold applies to Logs; there is no separate Logs freshness
alias and no fixed 20-second threshold. Changing it does not change scan cadence.

Legacy `api_ip_freshness=0`/`Auto` is saved once as its previous effective value:
`max(60, CHECK_INTERVAL)` at migration. An existing stored IP Source setting
without a freshness setting is treated the same way unless an explicit ENV
freshness is supplied. Later interval changes do not change the saved threshold.
If migration cannot be saved, configuration is marked degraded and enforcement
waits for a successful retry. Positive stored thresholds keep their exact value.

The old `api_ip_candidate_mode` and `api_ip_online_window` rows remain readable
for backup compatibility, but no longer alter collection: API mode always uses
`online=true`. No database schema migration is required.

## Live pagination and collection progress

All `/api/users` HTTP attempts share one queue, including background full sync,
online candidates, membership checks and retries. Each attempt waits for the
previous response or error, then pauses one second before sending. Slow panel
responses therefore lower the rate automatically; there is no Telegram setting.
Online-IP request concurrency is independent of this page queue.

The panel's online population can change while `/api/users` pages are being
read. Candidate collection uses a deterministic username order and validates
each page, while accepting a change in the reported total between pages. It
deduplicates overlapping identities and fetches at most one extra batch of
tail pages. Conflicting identities, malformed or missing pages and rejected
credentials still invalidate the query. Newly arriving users beyond that
bounded read can be observed in the next cycle.

A changed listing is evidence about the users actually returned; it is not a
complete membership snapshot. If either the initial candidate list or the
confirmation list changes, unobserved users' pending warnings are preserved.
Observed users still need valid IP evidence and matching current group/core
scope. The background full-user sync can refresh valid returned users but
defers deletion of local membership when its listing changes; that incomplete
sync does not advance the last completed-sync time. No counters or Trust
history are reset to recover from pagination churn.

At the normal INFO log level, online-IP collection prints a starting count,
progress at most once every ten seconds as requests finish, and a final
summary. For example:

```text
API IP requests: 800/1000 (80.0% complete), success 97.5% (780 ok), 5 not found, 5 forbidden, 10 failed (2 unauthorized, 3 timeouts: 2 read, 1 connect, 0 pool, 0 write), 0 unsent/deferred
```

Completion and success are different percentages. Success is the fraction of
completed lookups that returned a valid online-IP response. Confirmed missing
users count toward coverage but not toward successful IP responses; rejected
keys and failures remain visible in the totals. Timeouts are a subset of failed
lookups. Unsent/deferred users remain unknown and do not contribute to coverage
or the successful-response percentage.
Successful responses still need the per-user evidence checks described above.

An open online-stat circuit breaker holds the remaining queue instead of
discarding it. After the cooldown, one probe can resume normal bounded
concurrency. Total cooldown waiting per collection is limited to 65 seconds,
independently of evidence freshness: queued work has not yet made an observation.
Responses retain their own observation windows and freshness checks before use.
Active requests retain their own timeout. Persistent outages leave remaining users deferred for
the next cycle. Cancellation or changed monitoring/configuration stops the wait
and cancels pending requests. User synchronization uses its own breaker.
Individual collection request URLs, status codes and transport errors are
DEBUG details. Other panel operations retain their normal request logging.

The default `api_ip_timeout` is 20 seconds; an explicitly saved value takes
precedence. Upgrading the code does not overwrite a stored timeout. This is the
HTTP client's timeout for each transport phase, not a deadline for a whole
collection. Collection start logs include the effective timeout, concurrency and
breaker wait budget. Aggregate counters distinguish read, connect, pool and
write timeouts; unclassified timeouts still count toward the timeout total.

Full user synchronization runs in a separate background task. Its timer starts
after the previous sync returns, and it can run while IP collection is active.
The shared one-second page gate serializes `/api/users` attempts (including
candidate pages, sync pages and membership batches), not whole collection cycles
or `/api/node/online_stats/{id}/ip` requests. The sync and IP collection locks
each prevent duplicate instances of their own operation; they do not exclude
one another.
