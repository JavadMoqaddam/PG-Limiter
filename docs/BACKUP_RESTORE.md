# Backup and offline restore

In Telegram, open **Settings → Backup & Restore** for backup scheduling, **Backup Now** (available when Auto-Backup is enabled), and **Restore**. Restore remains available when Auto-Backup is disabled. Immediate and scheduled backups use the same delivery path: the configured backup topic, with administrator delivery as a fallback.

A confirmed Telegram document receipt remains successful even if local cleanup fails. Administrator fallback is allowed for a definite group destination rejection; an uncertain group send does not trigger another copy or claim success. With Topics disabled or no group configured, the existing administrator delivery path still applies. See [Telegram delivery and retries](TELEGRAM_DELIVERY.md).

To create a backup on the host:

```bash
pg-limiter backup /root/pg-limiter-backup.zip
```

New archives contain a **database-consistent SQLite backup API snapshot** of the configured `DATABASE_URL`. Committed WAL records are included in that snapshot; live `-wal` and `-shm` files are never archived. Missing or uninitialized databases fail the backup rather than producing a successful database-backup message. External databases and SQLite `file:` URI URLs require a database-specific backup procedure.

The archive also includes the environment file once and the actual persisted warning/trust history, punishment compatibility history, original groups, Telegram topics/message caches, pending notifications, disable-message receipts, automatic-backup settings and existing legacy import inputs. JSON and environment files are captured independently of SQLite: the archive does **not** promise a transaction spanning those stores. A versioned manifest records checksums and explicitly absent JSON files.

[Mirza Bot notification](MIRZA_NOTIFICATIONS.md) bot settings, named messages and their numbered step assignments, admin memberships, API keys and delivery states are part of the SQLite snapshot. Pending punishment evidence, including the selected message identity, is retained in the existing fact-journal sidecar. Backup archives therefore contain credentials and should be kept private.

The Telegram delivery policy introduces no additional schema migration or new backup format. Critical pending notifications retain their existing JSON backup inclusion. Replaying restored pending state can duplicate a critical notification whose Telegram acknowledgement was lost; noncritical pending sends and node-summary state are not durable recovery records.

The original-group compatibility file now defaults to `$PG_LIMITER_DATA_DIR/user_groups_backup.json` (`/var/lib/pg-limiter/user_groups_backup.json` by default). Until that file exists, the old working-directory or `/app/.user_groups_backup.json` is read without being removed. Subsequent saves persist on the volume.

## Stage and apply

Send `/restore` and upload a ZIP or supported legacy configuration JSON. The bot validates it and stages a file with owner-only permissions beneath `/var/lib/pg-limiter/restore-staging`. It reports the exact host path and command. **Uploading does not replace live files, import settings, reload stores or restart the daemon.**

Run the reported command from the host terminal:

```bash
pg-limiter restore /var/lib/pg-limiter/restore-staging/GENERATED_NAME.zip
```

The management command validates the archive and destination before asking to apply it or stopping the service. It checks archive paths, duplicate/symlink members, manifest completeness, checksums, JSON and SQLite integrity. It then stops the service and verifies every Compose service container ID discovered before and after stop; failed discovery/inspection or any running container prevents application, including with custom container names. The shared offline helper validates again, prepares replacements, applies database/configuration/state files, and removes stale target WAL/SHM files. File-application failures attempt rollback for every affected target. If a target cannot be recovered, its original recovery copy is retained and its path is reported for offline recovery; the service remains stopped. Explicitly absent JSON state in a new backup removes newer state at that destination. Absent original groups are stored as an empty persisted record so the retained legacy copy cannot reappear on startup.

Restored `.env`'s explicit SQLite `DATABASE_URL` selects the database location for the next lifecycle. Without an archived URL, the current effective URL selects the destination. An archived `PG_LIMITER_DATA_DIR` also selects the original-group and legacy-config destinations using the same path rules as their runtime owners, including explicit absence. The helper checks that database and JSON state destinations are on real persistent mounts; an ephemeral image path cannot produce a successful restore. Directory mounts and file bind mounts for the configured database or owned state files are supported. `DATABASE_URL` and `PG_LIMITER_DATA_DIR` using environment interpolation must be resolved to explicit values before restoration. Configuration is writable only in the offline apply helper; the daemon's configuration mount stays read-only.

After applying, the command asks whether to start the service. Declining leaves it stopped. Starting uses a fresh process and configuration; the bot never swaps files underneath a running database engine. The management script and application image must both contain this backup format/helper; an older image fails before shutdown because it cannot run the validator.

## Rollback across migrations

For rollback across a database migration, keep a complete private archive made
before the upgrade, together with the previous image identity, Compose file and
management script. The updater's limited copies do not replace this archive,
especially for a custom database path and persisted state sidecars. Apply the
archive with the current offline restore helper and decline its restart prompt;
then restore the matching previous image, Compose file and script before starting.
An older image cannot resolve newer Alembic revisions. Revision `013` also
refuses every in-place downgrade because 1.7.3 cannot read grouped notification
contexts in the database and punishment-fact journal. See the
[1.7.4 rollback procedure](releases/v1.7.4.md#rollback) for returning to 1.7.3,
or the [1.7.3 rollback procedure](releases/v1.7.3.md#rollback) for migration `012`.

## Legacy compatibility

Old bot ZIPs with `data/pg_limiter.db` and host ZIPs with `data/data/pg_limiter.db` are recognized. A unique custom SQLite filename under `data/` is also recognized. If an old archive contains an associated WAL, it is materialized into a standalone checked temporary snapshot before application. Its original cross-file consistency cannot be certified. An old archive with no database, multiple database candidates or corrupt data is refused. Legacy JSON files that were never included in that archive remain unchanged, and this limitation is reported; undeclared deployment/log files are not restored.

Standalone legacy configuration JSON is staged by `/restore` and imported by the same offline host command. This preserves the existing supported form: `disable_method`, nonempty `disabled_group_id`, `enhanced_details`, special limits and the root whitelist. The import uses one canonical database transaction; invalid limits below 1 are skipped and reported. Missing local users are created for their imported settings without contacting the panel. Other configuration sections and panel credentials are not imported by that legacy form. The result is reported as a configuration import, not as restoration of a database snapshot.
