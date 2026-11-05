# Mirza customer notifications

Open **Settings → 🔔 Notifications** in a private chat with the PG-Limiter bot.
Existing authorized bot administrators can configure a separate Mirza sales bot
for each PasarGuard admin. New configurations are disabled until completed and
explicitly enabled.

## Set up an admin

1. Choose **Add Admin** and enter the exact **PasarGuard admin username** that
   owns the customer's configuration. This does not create a panel admin.
2. Set that admin's Mirza API base URL, for example
   `https://bot.example.com/api/`. An HTTPS domain without a path is normalized
   to `/api`. Enter the API base, not its documentation page or `/users` endpoint.
3. Set a Mirza API key authorized to use the **users API**, including its
   `send_message` action. Use the key generated in Mirza's API-key settings.
   The menu shows only whether a key is configured; replacing it does not reveal
   the previous value.
4. Set the message and select the punishment stages that should notify.
5. Enable the configuration.

Repeat these steps for each sales admin. Each configuration has its own URL,
key, message and trigger selection; another admin's bot is never used as a
fallback. The panel key used by PG-Limiter needs appropriately scoped
`users.read` permission to read users across the configured admins. Keep the
existing enforcement permissions described in [Panel API keys](PANEL_API_KEY.md).

Enter keys only in the private bot chat. PG-Limiter deletes an incoming key
message best-effort after saving, but cannot guarantee deletion. Use `/cancel`
to cancel a pending edit. A new command or menu also clears that edit.

Configured admins are listed ten per page. **Delete** requires confirmation;
it removes the notification configuration, not the PasarGuard admin.

## Message template

Use these literal, case-sensitive placeholders:

| Variable | Value |
| --- | --- |
| `[Username]` | Username of the configuration that was punished |
| `[Limit]` | Its effective allowed concurrent-connection count |

```text
کانفیگ [Username] به‌دلیل تخلف غیرفعال شد.
```

An example using both variables:

```text
کانفیگ [Username] حداکثر برای اتصال هم‌زمان [Limit] کاربر مجاز است؛ لطفاً از این تعداد بیشتر نشود.
```

Every occurrence is replaced. `[Username]` comes from the punishment incident's
configuration username, not the customer's Telegram handle. `[Limit]` is captured
with the punishment decision: a user-specific limit takes priority over an
applicable group limit, followed by the general limit. Later setting changes or
moving the user into a disabled group do not change that saved value. If a
template needs `[Limit]` and no valid limit is available, the notification fails
without guessing a number. A template without variables is sent unchanged.
Other bracketed text remains literal.

Mirza supports HTML messages, so a template can contain
`<code>[Username]</code>`. The substituted username is HTML-escaped. The final
message must fit within 4096 UTF-16 units; an oversized result fails without
being truncated or sent in parts.

## Choose punishment stages

- **Last step** follows the final stage of the punishment ladder in effect for
  that incident. It does not assume that the final stage is number five.
- **Selected steps** lets you choose one or more stage positions by toggling
  their checkmarks independently in **Triggers**. Selecting the first step
  switches from dynamic last to selected mode. An enabled configuration must
  retain at least one selected step; choose **Last step** or disable it before
  clearing the final selection. Review selections if you reorder or remove
  punishment stages. Removed positions stay visible as inactive and can be
  removed; they are never silently reassigned to another position.

Every newly applied matching punishment creates a notification, including each
new application of the final step to the same configuration. Replaying an
incident or reconciling its history record does not create another notification.
Enabling notifications does not send historical punishments retroactively.

Disable and Revoke + Disable stages qualify after confirmed disabling. Revoke
and UUID-reset errors and retries remain part of panel enforcement. Warning-only
punishment stages qualify after their violation record is durably confirmed.
Ordinary scan-warning reports and plain disables with smart punishment turned
off do not trigger this feature.

## Find the customer and send

The notification worker reads the current full user response from PasarGuard.
It reads `admin.username` and `note` together, matches the admin's configuration,
and extracts the customer's numeric Telegram ID from a note such as:

```text
5123456789 | example_user | buy
```

Only the first field is used as the recipient. It must contain a positive ASCII
numeric ID. Missing or malformed notes, missing owners and unconfigured admins
are skipped; the bot does not guess a recipient. Owner and note are resolved
when the queued notification is processed, so changes made on the panel before
that lookup affect the resolution.

The request is `POST <API base>/users`, with the admin's key in the `Token`
header and JSON containing `actions: "send_message"`, `chat_id` as a string,
and the rendered `text`. Delivery is acknowledged only when Mirza returns a
successful HTTP response with the JSON boolean `status: true`. This is Mirza's
acknowledgment, not proof that the customer read the Telegram message.

## Delivery status and changes

The menu shows delivery status counts. Sending runs independently of the
limiter, so a slow or unavailable Mirza endpoint does not delay punishment.

| Status | Meaning |
| --- | --- |
| Pending | Waiting to resolve user details and the recipient |
| Ready | Recipient and message are saved, waiting for transmission |
| Sending | A transmission is in progress; interruption can leave an uncertain outcome |
| Retry | A definite temporary failure is waiting for a bounded retry |
| Delivered | Mirza acknowledged the request |
| Skipped | No valid recipient/owner or the applicable settings changed |
| Failed | Invalid configuration/payload, rejected request or retries exhausted |
| Uncertain | The remote request may have succeeded; it is not resent automatically |

Temporary failures known to occur before sending and explicit rate-limit
responses can be retried, with at most three attempts for metadata resolution
and three transmission attempts for definite temporary failures. A timeout after
sending, interrupted request or ambiguous server response can follow successful delivery. Mirza documents no
idempotency key, so such requests remain uncertain instead of being blindly
repeated. This avoids that source of duplicate messages at the cost of some
uncertain messages potentially not arriving.

Editing, disabling or deleting an admin configuration invalidates its older
unsent events. Each edit changes the configuration revision; an old button or
pending text edit cannot overwrite a newer revision. A request already in
flight may complete. New incidents use the updated settings. A restart retains pending events and does not automatically
repeat an interrupted in-flight request.

## Storage and upgrades

Migration `012_mirza_notifications` adds route and delivery tables and extends
punishment evidence; existing facts are not backfilled. Container startup applies
the migration. For a local Python installation, run `python -m alembic upgrade
head` against the configured `DATABASE_URL` before starting the updated daemon.

Routes, API keys and notification delivery state are included in the existing
SQLite backup. The existing punishment-fact journal retains pending evidence
during a temporary database failure and is also included in backups. Treat
backup archives as credentials. See [Backup & Restore](BACKUP_RESTORE.md).
