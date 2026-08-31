"""
JSON to Database Migration Script
Migrates data from JSON files to SQLite database.

Run this script once after upgrading to the database version:
    python -m db.migrate_from_json
"""

import asyncio
import json
import os
import sys
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_db
from db.crud import (
    UserLimitCRUD,
    ExceptUserCRUD,
    DisabledUserCRUD,
    ViolationHistoryCRUD,
    ConfigCRUD,
)
from utils.logs import logger

# Where the retired files actually live. These defaults used to be bare relative
# dot-names (".disable_users.json"), which resolve against the working directory -
# /app in the container - while the real files are on the persisted volume. Every
# importer therefore reported "file not found" and did nothing, and start.sh's gate
# tested the same wrong paths, so the whole module was unreachable.
LEGACY_DIR = os.environ.get("PG_LIMITER_DATA_DIR", "/var/lib/pg-limiter")
LEGACY_DISABLED_USERS = os.path.join(LEGACY_DIR, "disable_users.json")
LEGACY_VIOLATION_HISTORY = os.path.join(LEGACY_DIR, "violation_history.json")
LEGACY_USER_GROUPS = os.path.join(LEGACY_DIR, "user_groups_backup.json")
LEGACY_CONFIG = os.path.join(LEGACY_DIR, "config.json")


async def migrate_config(config_file: str = LEGACY_CONFIG):
    """Migrate config.json to database Config table."""
    if not os.path.exists(config_file):
        logger.warning(f"Config file not found: {config_file}")
        return 0
    
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read config file: {e}")
        return 0
    
    count = 0
    async with get_db() as db:
        # Migrate panel settings
        if "panel" in config:
            await ConfigCRUD.set(db, "panel", config["panel"])
            count += 1
        
        # Migrate telegram settings
        if "telegram" in config:
            await ConfigCRUD.set(db, "telegram", config["telegram"])
            count += 1
        
        # Migrate limits
        if "limits" in config:
            limits = config["limits"]
            
            # General limit
            if "general" in limits:
                await ConfigCRUD.set(db, "general_limit", limits["general"])
                count += 1
            
            # Special limits
            special_limits = limits.get("special", {})
            for username, limit in special_limits.items():
                await UserLimitCRUD.set_limit(db, username, limit)
                count += 1
            
            # Except users
            except_users = limits.get("except_users", [])
            for username in except_users:
                await ExceptUserCRUD.add(db, username, reason="Migrated from config.json")
                count += 1
        
        # Also check for old-style except_users at root level
        if "except_users" in config and isinstance(config["except_users"], list):
            for username in config["except_users"]:
                await ExceptUserCRUD.add(db, username, reason="Migrated from config.json")
                count += 1
        
        # Migrate timing settings
        if "timing" in config:
            await ConfigCRUD.set(db, "timing", config["timing"])
            count += 1
        elif "check_interval" in config or "time_to_active_users" in config:
            timing = {
                "check_interval": config.get("check_interval", 60),
                "time_to_active_users": config.get("time_to_active_users", 900),
            }
            await ConfigCRUD.set(db, "timing", timing)
            count += 1
        
        # Migrate display settings
        if "display" in config:
            await ConfigCRUD.set(db, "display", config["display"])
            count += 1
        
        # Migrate API settings
        if "api" in config:
            await ConfigCRUD.set(db, "api", config["api"])
            count += 1
        
        # Migrate country_code
        if "country_code" in config:
            await ConfigCRUD.set(db, "country_code", config["country_code"])
            count += 1
        
        # Migrate disable_method
        if "disable_method" in config:
            await ConfigCRUD.set(db, "disable_method", config["disable_method"])
            count += 1
        
        if "disabled_group_id" in config:
            await ConfigCRUD.set(db, "disabled_group_id", config["disabled_group_id"])
            count += 1
        
        # Migrate group_filter
        if "group_filter" in config:
            await ConfigCRUD.set(db, "group_filter", config["group_filter"])
            count += 1
        
        # Migrate punishment settings
        if "punishment" in config:
            await ConfigCRUD.set(db, "punishment", config["punishment"])
            count += 1
    
    logger.info(f"Migrated {count} config items from {config_file}")
    return count


async def migrate_disabled_users(disabled_file: str = LEGACY_DISABLED_USERS):
    """
    Import the retired disabled-users file.

    Delegates to ``utils.handel_dis_users.import_legacy_json``, which is the
    canonical importer: it writes to the ``users`` table (the single store since
    Redis was removed), reads all three historical key shapes, never resurrects a
    ban an operator has already lifted, and renames the file when it is done.

    This function used to write to ``DisabledUserCRUD`` - the legacy
    ``disabled_users`` table that nothing reads any more - so its work was
    invisible to enforcement.
    """
    from utils.handel_dis_users import import_legacy_json

    count = await import_legacy_json(disabled_file)
    logger.info(f"Imported {count} disabled users from {disabled_file}")
    return count


async def migrate_user_groups(groups_file: str = LEGACY_USER_GROUPS):
    """Migrate user groups backup from JSON to database."""
    if not os.path.exists(groups_file):
        logger.warning(f"User groups file not found: {groups_file}")
        return 0
    
    try:
        with open(groups_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read user groups file: {e}")
        return 0
    
    count = 0
    async with get_db() as db:
        user_groups = data.get("user_groups", {})
        
        for username, info in user_groups.items():
            groups = info.get("groups", [])
            
            # Check if user is disabled and update their original_groups
            disabled = await DisabledUserCRUD.get(db, username)
            if disabled:
                disabled.original_groups = groups
                count += 1
    
    logger.info(f"Migrated {count} user group backups from {groups_file}")
    return count


async def migrate_violation_history(violations_file: str = LEGACY_VIOLATION_HISTORY):
    """
    Import the retired violation-history file, timestamps intact.

    Two things used to make this dangerous to run:

    * every record was written with ``timestamp=time.time()``, so an imported
      history looked like it all happened this second. The escalation step is the
      number of violations inside the window, so that sent every affected user
      straight to the harshest punishment.
    * it was not idempotent, so a second run doubled everyone's count.

    Now the original timestamp is preserved, records without a usable one are
    skipped rather than invented, and the whole import is a no-op when the table
    already holds rows - which is the normal state on any installation that has
    been recording violations in SQLite.
    """
    if not os.path.exists(violations_file):
        logger.warning(f"Violation history file not found: {violations_file}")
        return 0

    try:
        with open(violations_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read violation history file: {e}")
        return 0

    count = 0
    skipped = 0
    async with get_db() as db:
        existing = await ViolationHistoryCRUD.count_all(db)
        if existing:
            logger.info(
                f"Skipping violation import: {existing} rows are already in SQLite, "
                f"and importing on top of them would double every user's count"
            )
            return 0

        violations = data.get("violations", {})

        for username, records in violations.items():
            for record in records:
                original = record.get("timestamp")
                try:
                    original = float(original)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if original <= 0:
                    skipped += 1
                    continue

                await ViolationHistoryCRUD.add(
                    db,
                    username=username,
                    step_applied=record.get("step_applied", 0),
                    disable_duration=record.get("disable_duration", 0),
                    timestamp=original,
                )
                count += 1
        await db.commit()

    if skipped:
        logger.warning(f"Skipped {skipped} violation records with no usable timestamp")
    logger.info(f"Migrated {count} violation records from {violations_file}")
    return count


async def backup_json_files():
    """
    Copy the retired files aside before importing them.

    The backup directory used to be a bare relative ``backup_json/``, which in the
    container is /app - not on a volume, so the copies vanished with the next
    image. It now sits next to the files it is protecting.
    """
    backup_dir = os.path.join(LEGACY_DIR, "backup_json")
    os.makedirs(backup_dir, exist_ok=True)

    files_to_backup = [
        LEGACY_CONFIG,
        LEGACY_DISABLED_USERS,
        LEGACY_USER_GROUPS,
        LEGACY_VIOLATION_HISTORY,
    ]

    import shutil

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for path in files_to_backup:
        if os.path.exists(path):
            backup_path = os.path.join(
                backup_dir, f"{os.path.basename(path)}.{timestamp}.bak"
            )
            shutil.copy2(path, backup_path)
            logger.info(f"Backed up {path} to {backup_path}")


async def import_legacy_stores() -> int:
    """
    Import only the two retired per-user stores: disabled users and violation
    history.

    Deliberately does not touch config. ``main()`` also feeds config.json into the
    Config table, which on a live installation would overwrite the current settings
    - panel credentials, limits, group filter, punishment steps - with whatever an
    old file happens to contain. That is a reasonable one-shot upgrade step to run
    by hand. It is not something to do on every container start, and start.sh calls
    this function, not ``main()``, for exactly that reason.
    """
    await init_db()

    total = 0
    total += await migrate_disabled_users()
    total += await migrate_violation_history()
    return total


async def main():
    """Run the full migration."""
    print("=" * 60)
    print("PG-Limiter: JSON to Database Migration")
    print("=" * 60)
    
    # Initialize database
    print("\n1. Initializing database...")
    await init_db()
    print("   ✓ Database initialized")
    
    # Backup JSON files
    print("\n2. Backing up JSON files...")
    await backup_json_files()
    print("   ✓ JSON files backed up to backup_json/")
    
    # Migrate data
    print("\n3. Migrating data...")
    
    config_count = await migrate_config()
    print(f"   ✓ Migrated {config_count} config items")
    
    disabled_count = await migrate_disabled_users()
    print(f"   ✓ Migrated {disabled_count} disabled users")
    
    groups_count = await migrate_user_groups()
    print(f"   ✓ Migrated {groups_count} user group backups")
    
    violations_count = await migrate_violation_history()
    print(f"   ✓ Migrated {violations_count} violation records")
    
    total = config_count + disabled_count + groups_count + violations_count
    
    print("\n" + "=" * 60)
    print(f"Migration complete! Total items migrated: {total}")
    print("=" * 60)
    print("\nYour JSON files have been backed up next to them, under backup_json/")
    print("You can safely delete them after verifying the migration.")
    print("\nNote: Panel credentials and bot token are now in .env file.")
    print("Dynamic settings are stored in the database.")


if __name__ == "__main__":
    if "--legacy-only" in sys.argv:
        imported = asyncio.run(import_legacy_stores())
        print(f"Legacy store import finished: {imported} records")
    else:
        asyncio.run(main())
