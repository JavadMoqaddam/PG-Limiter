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
by current node/group/core and user-membership checks. A changed scope, failed
or incomplete response prevents punishment and preserves the warning. A fresh
within-limit response also prevents the pending punishment. Repeated guards
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

A failed request, malformed body, missing expected node or invalid timestamp
protects the affected user for that cycle: no warning, ban
or normalization decision is made, and existing warning counters remain.
Node-ID aliases that normalize to the same ID, such as `1` and `01`, or IP keys
that become identical after trimming whitespace make the whole user's payload
malformed. They cannot overwrite conflicting evidence and clear a warning.
Users with complete evidence are evaluated when the HTTP answer-rate gate allows
the cycle. An incomplete user is skipped entirely, including when the visible
IPs already exceed their limit. Their warning counter, Trust and history remain. An 80% answer rate never makes the remaining
20% known.

Required nodes are resolved for each user from their enabled PasarGuard groups,
those groups' inbound tags, and the effective core configuration of each included
node. Include is the limiter's global monitoring selection; it does not grant
every user access to every included node. Multiple groups contribute the union
of their nodes. Node names such as `pm-` and the keys returned by the IP endpoint
are not used to infer authorization.

Only connected nodes in that user's applicable scope require a response.
An `error`/`connecting` node does not stop users being checked on healthy nodes,
including users in the same group. This counts the healthy monitored scope;
it does not prove that the excluded node has no live client connections.
Missing/null data from an expected healthy node still protects that user. If
none of a user's applicable nodes are healthy, their evidence remains unknown.

Group memberships arrive with the existing online-user list. Group/core metadata
is shared across users, and each group combination shares its node calculation.
Nodes, groups and cores are read before collection and confirmed after IP
preparation; the paged online-user list is read again to confirm memberships.
No separate per-user permission request is added. Observed membership or node-scope
changes retire only the affected users. Failed topology/permission requests
cannot become empty scopes; check `groups.read` and `cores.read` permissions.

A valid empty map (or expired activity timestamps on WireGuard/legacy cores) can clear warnings when all
of the user's required healthy-node maps are present. During a fleet outage or
topology change, retained warning users absent from the candidate list are
protected as well. A complete empty candidate list can clear warnings only when
the monitored fleet/topology remains available and unchanged through the query.
Failure to obtain the candidate list, fleet or required topology skips the cycle.
Fleet node-coverage percentages are diagnostic; the legacy
`api_ip_min_node_coverage` value no longer vetoes complete per-user evidence.

API batches are never merged with earlier cycles or Logs, and a settings change
invalidates an in-flight batch without resetting warnings. **Last API Cycle** shows whether the sample
was complete, partially usable, or skipped.

## Reading Last API Cycle

Successful requests describe HTTP transport, not complete user evidence. **Users
missing required node evidence** counts users whose expected node keys are absent.
Expected nodes are the union of the per-user required sets evaluated in the batch.
**Nodes reporting across responses** counts expected node IDs with valid maps
present anywhere in the batch; **Nodes absent across all responses** is expected
nodes minus that union. Unexpected node IDs cannot compensate for a missing
expected node.
The diagnostic `missing_node_ids` is instead the union of per-user gaps and
does not mean those nodes were missing for every user. A node can report for
one user and still be missing for another.

When an HTTP failure or user-coverage gate returns before node-map evaluation,
the report retains the known expected fleet and unknown-user count. With
`node_evidence_evaluated=false`, reporting nodes and global absence are unknown;
zero diagnostic counts do not establish that every node is absent or that no
user has missing node evidence.

The report retains unknown users, stale observations, elapsed collection time
and `consecutive_unsuccessful_cycles` as separate measures. A partial collection
can still enforce known users and resets the unsuccessful-cycle streak.
The existing three-skip coverage alert counter is separate from the two-cycle
fallback counter. Missing-node protection
and activity-timestamp filtering are different checks. Slow fanout does not expire
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
cycle continues evaluating complete users and does not trigger global fallback.

All requests can return HTTP 200 while missing per-user node maps make every
user unknown. The checked panel producer can omit failed, None or user-not-found
node results; an omitted node is not a confirmed empty map. Two such
unsuccessful collections can trigger fallback despite successful HTTP transport.
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
coverage, missing-node and timestamp checks described above.

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
