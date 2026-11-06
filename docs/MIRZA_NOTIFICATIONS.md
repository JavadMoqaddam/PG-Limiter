# Mirza Bot customer notifications

Open **Settings → 🔔 Notifications** in a private chat with the PG-Limiter bot.
Existing authorized bot administrators can configure multiple Mirza Bot instances.
Each named bot has its own API URL, key and named messages, shared by all
PasarGuard admins included in that bot. Each message covers explicit numbered
punishment stages. New bots start disabled with no admins or messages.

## Set up a bot

1. Choose **Add Bot** and give it a recognizable name, such as `Sales Bot`.
2. Open **Admins**. All panel admins appear in a single column, like Admin Filter.
   Tap an admin to include them; tap a checked admin to remove them. Selection
   uses the exact PasarGuard owner username, not a Telegram handle. There is no
   manual admin entry or Exclude mode.
3. Set the bot's Mirza Bot API base URL, for example
   `https://bot.example.com/api/`. An HTTPS domain without a path is normalized
   to `/api`. Enter the API base, not its documentation page or `/users` endpoint.
4. Set a Mirza Bot API key authorized to use the **users API**, including its
   `send_message` action. Use the key generated in Mirza Bot's API-key settings.
   Select only **Users (`users`)**; full access and other sections are unnecessary.
   Mirza Bot grants access by section, so this also permits other users actions;
   its documented key settings have no separate send-message-only permission.
   The menu shows only whether a key is configured; replacing it does not reveal
   the previous value.
5. Open **Messages → Add Message**, give it a unique name, enter its **Text**,
   then select its numbered **Steps**. Add more messages for other stages.
6. Enable the bot after selecting at least one admin, setting its URL and key,
   and preparing at least one message with text and selected steps.

Repeat these steps for each sales bot. An admin can belong to only one bot,
including disabled bots. Admins assigned elsewhere show a lock and that bot's
name; remove them from the other bot before assigning them here. To remove the
last member of an enabled bot, disable the bot first. Unselected admins and new
panel admins receive no notifications until included.

**Refresh** reloads the panel list. If the panel is unavailable, existing members
remain visible and removable. Members missing from the current panel list are
marked accordingly. The menu shares the existing panel-admin cache.

Another bot is never used as a fallback. The panel key used by PG-Limiter needs
`admins.read` to list admins and appropriately scoped `users.read` permission to
read users across the included admins. Keep the existing enforcement permissions
described in [Panel API keys](PANEL_API_KEY.md).

Enter keys only in the private bot chat. PG-Limiter deletes an incoming key
message best-effort after saving, but cannot guarantee deletion. Use `/cancel`
to cancel a pending edit. A new command or menu also clears that edit.

**Rename** changes the bot's display name. **Delete** requires confirmation;
it removes the bot configuration and frees its admin assignments. It does not
delete PasarGuard admins or the external sales bot.

## Test a bot connection

Open the selected bot and choose **🔌 Test Connection**. It sends the literal
message `test` through that Mirza Bot to the Telegram account of the PG-Limiter
administrator pressing the button. Start the selected Mirza Bot first and make
sure you have not blocked it. The test uses the saved API URL and key; it also
works while notifications are disabled, before selecting admins or a template.

The result appears in PG-Limiter. Success means Mirza Bot acknowledged the API
request; check your chat with that bot to confirm the test arrived. A rejected
request points to the URL, key or `users` permission. A rejected message may need
the recipient to start or unblock the bot. Raw API errors and keys are never
shown. If the result is uncertain, check your chat before testing again.

Each click makes at most one request, with no automatic retry. Repeated clicks
while a test is running and for five seconds afterwards are suppressed for that
administrator. Tests do not enable notifications, change settings, or enter
punishment history and notification counters. Stale settings buttons cannot
start a test; a request already in flight may finish after an edit.

## Manage messages

**Messages** lists each named message and its assigned stage numbers. A message
has **Text**, **Steps**, **Rename** and confirmed **Delete** controls. Names must
be unique within that bot; another bot may use the same names.

New messages start with empty text and no steps. A message with empty text or no
assigned steps is a draft. Drafts do not stop other ready messages from sending,
and there is no separate message enable switch: the bot's enabled state applies
to all its ready messages. Step assignments remain exclusive even for drafts.
Disable the bot before deleting or clearing the text/steps of its last ready
message. Deleting a message frees its assigned stages.

## Message text

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
کانفیگ [Username] حداکثر برای [Limit] دستگاه مجاز است؛ لطفاً از این تعداد بیشتر نشود.
```

Every occurrence is replaced. `[Username]` comes from the punishment incident's
configuration username, not the customer's Telegram handle. `[Limit]` is captured
with the punishment decision: a user-specific limit takes priority over an
applicable group limit, followed by the general limit. Later setting changes or
moving the user into a disabled group do not change that saved value. If a
template needs `[Limit]` and no valid limit is available, the notification fails
without guessing a number. A template without variables is sent unchanged.
Other bracketed text remains literal.

Mirza Bot supports HTML messages, so a template can contain
`<code>[Username]</code>`. The substituted username is HTML-escaped. The final
message must fit within 4096 UTF-16 units; an oversized result fails without
being truncated or sent in parts.

## Choose punishment stages

Open a message's **Steps** picker. Each numbered stage shows the effective
punishment description. Checkmarks identify this message's assignments; a lock
and another message's name identify stages already assigned within this bot.
Remove a stage from that message before assigning it here.

One message can cover multiple stages. Each numbered stage can belong to only
one message in the bot. Unassigned stages send no notification. For example,
assign stages 1 and 2 to `Reminder`, stage 3 to `Temporary disable`, and stage 5
to `Final disable`; stage 4 sends no message unless explicitly assigned.

Assignments refer to explicit positions. There is no dynamic **Last step**
selector or automatic remapping when the ladder changes. Review assignments
after reordering or replacing punishment stages. Removed positions stay visible
as inactive and can be removed; they are never silently moved to a new position.

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
It reads `admin.username` and `note` together, finds the bot including that admin,
and extracts the customer's numeric Telegram ID from a note such as:

```text
5123456789 | example_user | buy
```

Only the first field is used as the recipient. It must contain a positive ASCII
numeric ID. Missing or malformed notes, missing owners and unconfigured admins
are skipped; the bot does not guess a recipient. Owner and note are resolved
when the queued notification is processed, so changes made on the panel before
that lookup affect the resolution.

The request is `POST <API base>/users`, with the selected bot's key in the `Token`
header and JSON containing `actions: "send_message"`, `chat_id` as a string,
and the rendered `text`. Delivery is acknowledged only when Mirza Bot returns a
successful HTTP response with the JSON boolean `status: true`. This is Mirza Bot's
acknowledgment, not proof that the customer read the Telegram message.

## Delivery status and changes

The menu shows delivery status counts. Sending runs independently of the
limiter, so a slow or unavailable Mirza Bot endpoint does not delay punishment.

| Status | Meaning |
| --- | --- |
| Pending | Waiting to resolve user details and the recipient |
| Ready | Recipient and message are saved, waiting for transmission |
| Sending | A transmission is in progress; interruption can leave an uncertain outcome |
| Retry | A definite temporary failure is waiting for a bounded retry |
| Delivered | Mirza Bot acknowledged the request |
| Skipped | No valid recipient/owner or the applicable settings changed |
| Failed | Invalid configuration/payload, rejected request or retries exhausted |
| Uncertain | The remote request may have succeeded; it is not resent automatically |

Temporary failures known to occur before sending and explicit rate-limit
responses can be retried, with at most three attempts for metadata resolution
and three transmission attempts for definite temporary failures. A timeout after
sending, interrupted request or ambiguous server response can follow successful delivery. Mirza Bot documents no
idempotency key, so such requests remain uncertain instead of being blindly
repeated. This avoids that source of duplicate messages at the cost of some
uncertain messages potentially not arriving.

Editing a bot's settings or membership, creating/editing/deleting one of its
messages, disabling it, or deleting it invalidates its older unsent events. All
messages share the bot's configuration revision; an old button or pending text
edit cannot overwrite a newer revision. Saving an unchanged value keeps the
revision. A queued event retains the specific message selected at the punishment
decision; changing stage assignments never redirects an admitted event. A request
already in flight may complete. New incidents use the updated settings. A restart
retains pending events and does not automatically repeat an interrupted in-flight
request.

## Storage and upgrades

Migration `014_notification_messages` converts each existing bot's shared text
into one entry named `Default message`. It preserves explicit stage assignments.
The former dynamic last selection becomes the final numbered stage of the
effective configured ladder once during upgrade; the built-in five-stage ladder
is used if no usable ladder is configured. A former last selection on a ladder
longer than ten supported stages receives no assignment and requires manual
selection. Review converted assignments before enabling
or relying on notifications.

Bot IDs, revisions, credentials, enabled states, membership, punishment facts and
delivery receipts are preserved. Previously queued events retain their original
message identity; new messages receive fresh IDs. Deleting all messages and
restarting or restamping the database does not recreate the migrated message.
Upgrading does not replay previous punishments.

Revision `014` refuses every in-place downgrade before changing the database.
Older code cannot read message identities in new notification contexts in SQLite
and the external punishment-fact journal. Rollback requires stopping the daemon,
restoring the complete pre-upgrade database and state backup offline, and then
starting the matching older image. An Alembic downgrade cannot perform that
rollback.

Migration `013_notification_bots` converts each pre-existing admin configuration
into one named bot containing that same admin. IDs, revisions, keys, settings,
enabled states and notification receipts are preserved. Bots are not automatically
merged when URLs match. Previously queued single-admin events remain supported;
upgrading does not replay past punishments. Bot membership is included in backups.

Revision `013` refuses in-place downgrade before changing the database. Version
1.7.3 cannot read the grouped notification contexts stored in SQLite and the
external punishment-fact journal, even when a bot has only one admin. Rollback
requires restoring a full pre-upgrade backup of the database and state files
offline before starting the older version.

The earlier migration `012_mirza_notifications` adds route and delivery tables
and extends punishment evidence; existing facts are not backfilled. Container
startup applies all pending migrations, including `014`. For a local Python
installation, run `python -m alembic upgrade head` against the configured
`DATABASE_URL` before starting the updated daemon.

Routes, API keys and notification delivery state are included in the existing
SQLite backup. The existing punishment-fact journal retains pending evidence
during a temporary database failure and is also included in backups. Treat
backup archives as credentials. See [Backup & Restore](BACKUP_RESTORE.md).

Before upgrading, retain a full private backup and the previous image identity,
Compose file and management script. Follow the
[1.7.4 rollback procedure](releases/v1.7.4.md#rollback) to restore the complete
pre-upgrade database and state offline before returning to 1.7.3. For older
upgrades, the [1.7.3 rollback procedure](releases/v1.7.3.md#rollback) describes
returning to 1.7.2; downgrading revision `012` deletes Mirza Bot settings, API keys
and delivery history and does not restore a complete snapshot.
