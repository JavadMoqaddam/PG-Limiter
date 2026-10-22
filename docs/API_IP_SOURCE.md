# Panel API IP source

Select **Settings → IP Source → Panel API** in Telegram. The panel account must
have `nodes:stats` permission. Collection queries active users with `online=true`
and the configured **Group Filter**. All Include group IDs are sent together;
an empty Include filter selects nobody. Group Limits set limits, not the query
scope. Exclusions, administrator filters and exceptions still apply.

**Last seen** sets a positive whole number of seconds, T. Edit it in the same
menu; the value is saved exactly, including values below 30 seconds. Each IP
qualifies only when its original Xray timestamp satisfies:

```text
0 <= evaluation time - source timestamp < T
```

At T=90, an observation 89 seconds old qualifies; observations 90 or 91 seconds
old do not. Future timestamps and invalid timestamps are unknown evidence.
Receipt time never replaces the source timestamp. Freshness is evaluated after
collection and asynchronous preparation, and again locally before decisions
when processing takes time. There is no additional panel request before a ban.

Xray updates this timestamp when a tracked connection opens. A long-lived
connection can therefore have an old timestamp. API observations can differ
from log-window counts; choose T for the behavior of your deployment. The
panel's online-user filter and Last seen apply to different data: the former
selects users, while the latter accepts individual IP observations.

`CHECK_INTERVAL` controls collection frequency independently of T. It does not
add a 30-second allowance or widen the candidate query. `MAX_WARNING_COUNT`,
the warning/punishment policy, /24 grouping and existing device counting rules
continue to apply. The API does not provide inbound information; use Logs when
you need inbound-specific observations.

## Incomplete evidence

A failed request, malformed body, unavailable node, missing expected node or
invalid timestamp protects the affected user for that cycle: no warning, ban
or normalization decision is made, and existing warning counters remain.
Successfully observed users can still be evaluated when the configured global
coverage gates allow the cycle. An 80% answer rate never makes the remaining
20% known.

A valid empty IP map and a map containing only valid expired timestamps are
successful observations and can clear warnings. Failure to obtain a complete
candidate list or the node list skips enforcement. API batches are never merged
with earlier cycles or Logs, and a settings change invalidates an in-flight
batch without resetting warnings. **Last API Cycle** shows whether the sample
was complete, partially usable, or skipped.

## Configuration and upgrades

`API_IP_FRESHNESS=60` supplies the default for new installations. Set any positive
whole number of seconds. The stored `api_ip_freshness` database setting takes
precedence over ENV; Telegram edits that stored setting. Invalid configuration
values are logged and fall back to the valid ENV value or the 60-second default.

Legacy `api_ip_freshness=0`/`Auto` is saved once as its previous effective value:
`max(60, CHECK_INTERVAL)` at migration. An existing stored IP Source setting
without a freshness setting is treated the same way unless an explicit ENV
freshness is supplied. Later interval changes do not change the saved threshold.
If migration cannot be saved, configuration is marked degraded and enforcement
waits for a successful retry. Positive stored thresholds keep their exact value.

The old `api_ip_candidate_mode` and `api_ip_online_window` rows remain readable
for backup compatibility, but no longer alter collection: API mode always uses
`online=true`. No database schema migration is required.
