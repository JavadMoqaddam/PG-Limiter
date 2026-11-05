# Telegram delivery and retries

PG-Limiter records delivery receipts and keeps each queued operation with its original destination. Telegram can accept a message even when its response is lost, so PG-Limiter does **not** promise exactly-once Telegram delivery. A local event ID or message cache cannot make a Telegram send atomic with local persistence.

| Outcome | Meaning |
| --- | --- |
| Pending | The dispatcher still owns the operation. A caller stopping its wait does not prove failure or cancel the receipt. |
| Delivered | Telegram acknowledged delivery. Local receipt bookkeeping can finish without sending again. |
| Uncertain | The request may have reached Telegram, but no positive receipt is available. This is not reported as delivered. |
| Failed or cancelled | The operation ended without a confirmed receipt. This alone does not authorize a replacement message. |

## Policy by notification

**Critical incident notifications** use durable event ownership and an at-least-once retry policy. They may retry after an ambiguous transport failure to favor delivery over avoiding duplicates. If Telegram accepted the first attempt and its reply was lost, a retry can produce a second visible message. Event deduplication prevents duplicate local admission; delivery still depends on Telegram availability, permissions and continued service operation.

**Periodic warning reports and ordinary system messages** stop automatic append retries after an ambiguous send. This reduces repeated messages but can omit a warning whose request never reached Telegram. Explicit rate limits and confirmed connection failures before transmission can still retry. A newer completed warning scan supersedes the older scan's unsent work for the same destination and configuration, including when the new scan has no warnings. An already-started older send may finish, but cannot keep retrying behind that newer scan. Other destinations and critical incidents are unaffected.

**Node summaries** keep one pending operation per monitoring scope, coalesce new status text, and update the known message when its receipt arrives. Pending or ambiguous edits keep the same message ID. A replacement is allowed only when Telegram definitely reports that the old message is unavailable. An uncertain initial append blocks automatic replacement for that scope. Monitoring resets, the two-hour SSE refresh and shutdown retire the old producer so late receipts cannot overwrite a new summary or launch follow-up edits. Telegram waits do not hold up log ingestion.

**Menus and Status & Reports cards** treat unchanged text as success. They append a replacement only after confirmed message unavailability; timeouts, connection errors, permission errors and rate limits do not trigger a fresh reply automatically.

**Backup delivery** preserves a positive document receipt even if local cleanup fails. An uncertain group send does not trigger an administrator copy or claim success. A definite destination rejection can use the administrator fallback. See [backup and offline restore](BACKUP_RESTORE.md).

New warning scans, user actions, scheduled reports, reconnect notices and fresh summary scopes are intentional source events. Repeated content from those events is separate from retrying one transport request; this policy does not suppress all messages with identical text. Daily reports retain their existing persisted receipt and uncertainty handling.

No database schema or configuration migration is required for these delivery changes. Critical pending notifications continue to use the existing JSON store, which is already included in backups. Noncritical pending operations and node-summary ownership are process-local; restoring an older backup cannot establish whether Telegram accepted a previously unacknowledged send.

## Background

The critical policy follows [Alertmanager's preference for duplicate critical alerts over missed alerts](https://github.com/prometheus/alertmanager/blob/47403693800ba753b79f61c0fbca5e7d1d74281c/docs/high_availability.md). [Stripe's idempotency documentation](https://docs.stripe.com/api/idempotent_requests) and [AWS's guidance on safe retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) explain why safe append retries require receiver-side support. These are architectural references, not claims that Telegram supports the same mechanism or that delivery was verified against a live service.
