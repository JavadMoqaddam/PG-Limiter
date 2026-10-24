"""
Backup and restore handlers for the Telegram bot.
Includes functions for creating and restoring backups.
"""

import asyncio
import html
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)
from utils.atomic_io import atomic_write_json
from utils.read_config import config_write_transaction
from utils.backup import create_backup, runtime_layout, stage_backup
from utils.backup_paths import AUTO_BACKUP_CONFIG_FILE

from telegram_bot.input_owner import reply_input_prompt
from telegram_bot.constants import RESTORE_CONFIG
from telegram_bot.handlers.admin import check_admin_privilege
from utils.logs import get_logger

backup_logger = get_logger("backup")
RESTORE_STAGING_DIR = "/var/lib/pg-limiter/restore-staging"

# Conversation state for migration
MIGRATE_WAITING_FILE = 100


def _validated_import_limit(value) -> int:
    """Reject an invalid import value before a user CRUD mutation can flush."""
    try:
        limit = int(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("A special limit must be an integer of 1 or greater") from error
    if isinstance(value, bool) or limit < 1:
        raise ValueError("A special limit must be an integer of 1 or greater")
    return limit


async def _prefetch_settings_users(data: dict) -> None:
    """Mirror missing settings users before the caller owns the write transaction."""
    from db.database import get_db
    from db.crud.users import UserCRUD
    from utils.user_sync import fetch_and_sync_single_user

    limits = data.get("limits", {})
    usernames = set(limits.get("special", {}))
    usernames.update(limits.get("except_users", []))
    usernames.update(data.get("except_users", []))
    if not usernames:
        return
    async with get_db() as db:
        missing = [username for username in usernames if await UserCRUD.get_by_username(db, username) is None]
    for username in missing:
        await fetch_and_sync_single_user(username)


def _sync_create_backup_zip() -> tuple[str, str, str]:
    """Create a consistent SQLite snapshot with independently captured JSON state."""
    temp_dir = tempfile.mkdtemp()
    zip_name = f"pg-limiter-backup-{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    zip_path = os.path.join(temp_dir, zip_name)
    try:
        create_backup(zip_path, runtime_layout(live_instances=True))
    except BaseException:
        shutil.rmtree(temp_dir)
        raise
    return zip_path, zip_name, temp_dir


async def send_backup(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Send a comprehensive backup zip file to the user."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    temp_dir = None
    try:
        await update.message.reply_text("📦 Creating backup... Please wait.")

        zip_path, zip_name, temp_dir = await asyncio.to_thread(_sync_create_backup_zip)

        # Send the zip file
        with open(zip_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=zip_name,
                caption=(
                    "✅ <b>Backup created successfully!</b>\n\n"
                    "📁 This backup includes:\n"
                    "• Configuration files\n"
                    "• Database-consistent SQLite snapshot\n"
                    "• Persisted JSON state, captured independently\n\n"
                    "💡 /restore validates and stages this file for offline restoration."
                ),
                parse_mode="HTML",
            )

        # Cleanup
        shutil.rmtree(temp_dir)

    except Exception as e:
        await update.message.reply_html(f"❌ <b>Error creating backup:</b>\n<code>{html.escape(str(e))}</code>")
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def restore_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the restore process by asking for the backup file."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    await reply_input_prompt(
        context,
        update.message.reply_html(
            "📥 <b>Restore from Backup</b>\n\n"
            "Please send your backup file (zip or json format).\n\n"
            "The bot validates and stages it. Apply it offline with the host command shown afterward."
        ),
    )
    return RESTORE_CONFIG


def _sync_restore_config_zip(file_content: bytearray):
    """Validate and stage an upload; application belongs to the offline host command."""
    return stage_backup(file_content, runtime_layout(live_instances=True), RESTORE_STAGING_DIR)


async def restore_config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stage ZIP or legacy JSON uploads without changing any live store."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check
    try:
        document = update.message.document
        if not document or not str(document.file_name or "").lower().endswith((".zip", ".json")):
            await update.message.reply_html("❌ Please send a .zip or .json backup. Use /restore to try again.")
            return ConversationHandler.END
        file = await document.get_file()
        content = await file.download_as_bytearray()
        staged = await asyncio.to_thread(_sync_restore_config_zip, content)
        await update.message.reply_html(
            "✅ <b>Backup verified and staged.</b>\n\n"
            "Apply it from the host terminal:\n"
            f"<code>pg-limiter restore {html.escape(str(staged))}</code>\n\n"
            "That command stops the service before applying the backup and asks before starting it again. "
            "Your live data has not been changed."
        )
    except Exception as error:
        await update.message.reply_html(
            f"❌ <b>Backup validation failed:</b>\n<code>{html.escape(str(error))}</code>\nUse /restore to try again."
        )
    finally:
        context.user_data["waiting_for"] = None
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATE BACKUP - Full JSON backup migration to SQLite
# ═══════════════════════════════════════════════════════════════════════════════


async def migrate_backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the backup migration process."""
    backup_logger.info(f"migrate_backup_start called by user {update.effective_user.id}")

    check = await check_admin_privilege(update)
    if check is not None:
        backup_logger.warning(f"User {update.effective_user.id} not admin for migrate_backup")
        return check

    message = update.message or update.callback_query.message

    await reply_input_prompt(
        context,
        message.reply_html(
            "📥 <b>Migrate JSON Backup to Database</b>\n\n"
            "This will import data from your old JSON backup files into the SQLite database.\n\n"
            "<b>Supported files:</b>\n"
            "• <code>config.json</code> - Configuration, limits, except users\n"
            "• <code>.disable_users.json</code> - Disabled users list\n"
            "• <code>.violation_history.json</code> - Violation records\n"
            "• Any JSON file with the above format\n\n"
            "📤 <b>Please send your JSON file now</b>\n\n"
            "<i>Send /cancel to abort</i>"
        ),
    )
    backup_logger.info("Waiting for user to send JSON file...")
    return MIGRATE_WAITING_FILE


async def migrate_backup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the uploaded JSON file and migrate it to database."""
    backup_logger.info(f"migrate_backup_handler called")
    backup_logger.info(f"  - Message: {update.message}")
    backup_logger.info(f"  - Document: {update.message.document if update.message else None}")
    backup_logger.info(f"  - Text: {update.message.text if update.message else None}")

    try:
        if not update.message:
            backup_logger.error("No message in update")
            return ConversationHandler.END

        if not update.message.document:
            backup_logger.warning("No document in message - prompting user again")
            await reply_input_prompt(
                context,
                update.message.reply_html(
                    "❌ Please send a <b>JSON file</b> (not text).\n\n"
                    "Make sure to send the file as a document attachment.\n"
                    "Use /cancel to abort."
                ),
            )
            return MIGRATE_WAITING_FILE  # Stay in the conversation

        file_name = update.message.document.file_name
        backup_logger.info(f"Received file: {file_name}")

        if not file_name.endswith(".json"):
            await update.message.reply_html("❌ Please send a .json file.\nUse /migrate_backup to try again.")
            return ConversationHandler.END

        await update.message.reply_text("⏳ Processing backup file...")

        # Download the file
        file = await update.message.document.get_file()
        file_content = await file.download_as_bytearray()
        backup_logger.info(f"Downloaded file, size: {len(file_content)} bytes")

        try:
            data = json.loads(file_content.decode("utf-8"))
            backup_logger.info(f"Parsed JSON, keys: {list(data.keys())[:10]}")
        except json.JSONDecodeError as e:
            backup_logger.error(f"JSON decode error: {e}")
            await update.message.reply_html(f"❌ Invalid JSON format:\n<code>{str(e)}</code>")
            return ConversationHandler.END

        # Migration statistics
        stats = {
            "config_items": 0,
            "special_limits": 0,
            "except_users": 0,
            "disabled_users": 0,
            "violations": 0,
            "errors": [],
        }

        try:
            from db import get_db
            from db.crud import (
                ConfigCRUD,
                UserCRUD,
                ViolationHistoryCRUD,
            )

            backup_logger.info("Database imports successful")
        except Exception as import_err:
            backup_logger.error(f"Failed to import database modules: {import_err}")
            await update.message.reply_html(f"❌ Database import error:\n<code>{str(import_err)}</code>")
            return ConversationHandler.END

        try:
            await _prefetch_settings_users(data)
            async with config_write_transaction(), get_db() as db:
                # ─────────────────────────────────────────────────────────
                # Detect file type and migrate accordingly
                # ─────────────────────────────────────────────────────────

                # Check if it's a config.json file
                if "panel" in data or "limits" in data or "timing" in data:
                    backup_logger.info("Detected config.json format")

                    # Panel settings are deliberately NOT stored. Authentication comes
                    # from .env, nothing ever read the "panel_backup" row this used to
                    # write, and data["panel"] carries the panel password - so the only
                    # thing that row achieved was keeping a plaintext copy of the
                    # password in the database. The reply below already tells the
                    # operator to put panel credentials in .env.

                    # Migrate limits
                    if "limits" in data:
                        limits = data["limits"]

                        # General limit
                        if "general" in limits:
                            await ConfigCRUD.set(db, "general_limit", limits["general"])
                            stats["config_items"] += 1

                        # Special limits
                        special = limits.get("special", {})
                        for username, limit in special.items():
                            try:
                                validated_limit = _validated_import_limit(limit)
                            except ValueError as e:
                                stats["errors"].append(f"Special limit {username}: {e}")
                                continue
                            result = await UserCRUD.set_special_limit(
                                db, username, validated_limit, fetch_from_panel=False
                            )
                            if result is None:
                                stats["errors"].append(f"Special limit {username}: user not found")
                            else:
                                stats["special_limits"] += 1

                        # Except users from limits
                        except_list = limits.get("except_users", [])
                        for username in except_list:
                            result = await UserCRUD.set_excepted(
                                db, username, True, reason="Migrated from config.json", fetch_from_panel=False
                            )
                            if result is None:
                                stats["errors"].append(f"Except user {username}: user not found")
                            else:
                                stats["except_users"] += 1

                # Root-level except_users
                if "except_users" in data and isinstance(data["except_users"], list):
                    for username in data["except_users"]:
                        result = await UserCRUD.set_excepted(
                            db, username, True, reason="Migrated from config.json", fetch_from_panel=False
                        )
                        if result is None:
                            stats["errors"].append(f"Except user {username}: user not found")
                        else:
                            stats["except_users"] += 1

                # Timing settings. read_config reads the flat check_interval and
                # time_to_active_users keys, never a "timing" blob, so writing the blob
                # restored nothing - the running process kept its old values while the
                # import reported success.
                timing = data.get("timing") if isinstance(data.get("timing"), dict) else None
                if timing is None and ("check_interval" in data or "time_to_active_users" in data):
                    timing = data
                if timing:
                    if "check_interval" in timing:
                        await ConfigCRUD.set(db, "check_interval", str(int(timing["check_interval"])))
                        stats["config_items"] += 1
                    if "time_to_active_users" in timing:
                        await ConfigCRUD.set(db, "time_to_active_users", str(int(timing["time_to_active_users"])))
                        stats["config_items"] += 1

                # Display settings. Same story as "timing": read_config reads the flat
                # enhanced_details key, never a "display" blob.
                display = data.get("display") if isinstance(data.get("display"), dict) else {}
                enhanced = data.get("enhanced_details", display.get("enhanced_details"))
                if enhanced is None:
                    enhanced = display.get("show_enhanced_details")
                if enhanced is not None:
                    await ConfigCRUD.set(db, "enhanced_details", str(enhanced).lower())
                    stats["config_items"] += 1

                # Disable method
                if "disable_method" in data:
                    await ConfigCRUD.set(db, "disable_method", str(data["disable_method"]))
                    stats["config_items"] += 1

                if "disabled_group_id" in data:
                    await ConfigCRUD.set(db, "disabled_group_id", str(data["disabled_group_id"]))
                    stats["config_items"] += 1

                # Country code
                if "country_code" in data:
                    await ConfigCRUD.set(db, "country_code", data["country_code"])
                    stats["config_items"] += 1

                # Punishment settings. read_config reads punishment_enabled,
                # punishment_steps and punishment_window_hours - never a "punishment"
                # blob - so restoring the blob silently left the escalation ladder at
                # whatever it already was.
                punishment = data.get("punishment")
                if isinstance(punishment, dict):
                    if "enabled" in punishment:
                        await ConfigCRUD.set(db, "punishment_enabled", "true" if punishment["enabled"] else "false")
                        stats["config_items"] += 1
                    if "window_hours" in punishment:
                        await ConfigCRUD.set(db, "punishment_window_hours", str(int(punishment["window_hours"])))
                        stats["config_items"] += 1
                    if isinstance(punishment.get("steps"), list):
                        await ConfigCRUD.set(db, "punishment_steps", json.dumps(punishment["steps"]))
                        stats["config_items"] += 1

                # Group filter. Same again: the flat keys are group_filter_enabled,
                # group_filter_mode and group_filter_ids.
                group_filter = data.get("group_filter")
                if isinstance(group_filter, dict):
                    if "enabled" in group_filter:
                        await ConfigCRUD.set(db, "group_filter_enabled", "true" if group_filter["enabled"] else "false")
                        stats["config_items"] += 1
                    if group_filter.get("mode") in ("include", "exclude"):
                        await ConfigCRUD.set(db, "group_filter_mode", str(group_filter["mode"]))
                        stats["config_items"] += 1
                    if isinstance(group_filter.get("group_ids"), list):
                        await ConfigCRUD.set(
                            db,
                            "group_filter_ids",
                            ",".join(str(int(gid)) for gid in group_filter["group_ids"]),
                        )
                        stats["config_items"] += 1

            # Check if it's a .disable_users.json file
            if "disabled_users" in data or "disable_user" in data or "enable_at" in data:
                backup_logger.info("Detected disable_users.json format")

                disabled_users = data.get("disabled_users", data.get("disable_user", {}))
                enable_at = data.get("enable_at", {})

                if isinstance(disabled_users, list):
                    # Old format: list of usernames
                    import time

                    current_time = time.time()
                    for username in disabled_users:
                        try:
                            await UserCRUD.set_disabled(
                                db,
                                username=username,
                                disabled=True,
                                disabled_at=current_time,
                                reason="Migrated from JSON backup",
                            )
                            stats["disabled_users"] += 1
                        except Exception as e:
                            stats["errors"].append(f"Disabled user {username}: {e}")
                elif isinstance(disabled_users, dict):
                    # New format: {username: timestamp}
                    for username, disabled_at in disabled_users.items():
                        try:
                            user_enable_at = enable_at.get(username)
                            await UserCRUD.set_disabled(
                                db,
                                username=username,
                                disabled=True,
                                disabled_at=disabled_at,
                                enable_at=user_enable_at,
                                reason="Migrated from JSON backup",
                            )
                            stats["disabled_users"] += 1
                        except Exception as e:
                            stats["errors"].append(f"Disabled user {username}: {e}")

            # Check if it's a .violation_history.json file
            if "violations" in data:
                backup_logger.info("Detected violation_history.json format")

                violations = data["violations"]
                for username, records in violations.items():
                    if isinstance(records, list):
                        for record in records:
                            try:
                                await ViolationHistoryCRUD.add(
                                    db,
                                    username=username,
                                    step_applied=record.get("step_applied", 0),
                                    disable_duration=record.get("disable_duration", 0),
                                )
                                stats["violations"] += 1
                            except Exception as e:
                                stats["errors"].append(f"Violation {username}: {e}")
        except Exception as db_err:
            backup_logger.error(f"Database error during migration: {db_err}")
            await update.message.reply_html(f"❌ Database error:\n<code>{str(db_err)}</code>")
            return ConversationHandler.END

        # Build result message
        total = (
            stats["config_items"]
            + stats["special_limits"]
            + stats["except_users"]
            + stats["disabled_users"]
            + stats["violations"]
        )

        result_msg = (
            f"✅ <b>Migration Complete!</b>\n\n"
            f"📊 <b>Statistics:</b>\n"
            f"• Config items: {stats['config_items']}\n"
            f"• Special limits: {stats['special_limits']}\n"
            f"• Except users: {stats['except_users']}\n"
            f"• Disabled users: {stats['disabled_users']}\n"
            f"• Violations: {stats['violations']}\n"
            f"─────────────────\n"
            f"<b>Total items:</b> {total}\n"
        )

        if stats["errors"]:
            result_msg += f"\n⚠️ <b>Errors ({len(stats['errors'])}):</b>\n"
            for err in stats["errors"][:5]:  # Show first 5 errors
                result_msg += f"• <code>{err}</code>\n"
            if len(stats["errors"]) > 5:
                result_msg += f"... and {len(stats['errors']) - 5} more\n"

        result_msg += "\n💡 <i>Changes take effect immediately, no restart needed.</i>"

        await update.message.reply_html(result_msg)
        backup_logger.info(f"Migration complete: {stats}")

    except Exception as e:
        backup_logger.error(f"Migration error: {e}")
        await update.message.reply_html(f"❌ <b>Migration failed:</b>\n<code>{str(e)}</code>")

    return ConversationHandler.END


async def migrate_backup_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Cancel the migration process."""
    await update.message.reply_text("❌ Migration cancelled.")
    return ConversationHandler.END


def _sync_create_auto_backup_zip(interval_hours: int) -> tuple[str, str, str]:
    """Use the same database-consistent format for scheduled backups."""
    return _sync_create_backup_zip()


async def send_automatic_backup() -> bool:
    """
    Send an automatic backup to the forum group (if topics enabled) or all admins.
    Called by scheduler based on auto-backup config.
    """
    from telegram_bot.main import application
    from telegram_bot.utils import check_admin
    from telegram_bot.topics import TopicType, get_topics_manager

    backup_logger.info("📦 Creating automatic backup...")

    temp_dir = None
    try:
        # Get auto-backup config for interval info
        config = await asyncio.to_thread(get_auto_backup_config)
        interval_hours = config.get("interval_hours", 1)

        # Create temp directory for backup
        zip_path, zip_name, temp_dir = await asyncio.to_thread(_sync_create_auto_backup_zip, interval_hours)

        # Get topics manager
        topics_manager = get_topics_manager()

        caption = (
            "💾 <b>Automatic Backup</b>\n\n"
            f"🕐 Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            "📁 Includes: Environment, database-consistent SQLite snapshot, and separately captured JSON state\n\n"
            f"💡 <i>Backups are sent every {interval_hours} hour(s)</i>"
        )

        # Try sending to forum group first if topics are enabled
        if topics_manager.enabled and topics_manager.group_id:
            thread_id = topics_manager.get_topic_id(TopicType.BACKUPS)
            try:
                with open(zip_path, "rb") as f:
                    await application.bot.send_document(
                        chat_id=topics_manager.group_id,
                        document=f,
                        filename=zip_name,
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=thread_id,
                    )
                backup_logger.info(f"✅ Automatic backup sent to forum group (thread={thread_id})")
                shutil.rmtree(temp_dir)
                return True
            except Exception as e:
                backup_logger.warning(f"⚠️ Failed to send backup to forum group: {e}, falling back to admins")

        # Fallback: send to all admins
        admins = await check_admin()
        delivered = 0

        for admin in admins:
            try:
                with open(zip_path, "rb") as f:
                    await application.bot.send_document(
                        chat_id=admin,
                        document=f,
                        filename=zip_name,
                        caption=caption,
                        parse_mode="HTML",
                    )
                backup_logger.info(f"✅ Automatic backup sent to admin {admin}")
                delivered += 1
            except Exception as e:
                backup_logger.error(f"❌ Failed to send backup to admin {admin}: {e}")

        # Cleanup
        shutil.rmtree(temp_dir)
        backup_logger.info("✅ Automatic backup completed successfully")
        return delivered > 0

    except Exception as e:
        backup_logger.error(f"❌ Automatic backup failed: {e}")
        return False
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-BACKUP SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════


_auto_backup_lock = asyncio.Lock()

# Default config: enabled with 1 hour interval
DEFAULT_AUTO_BACKUP_CONFIG = {"enabled": True, "interval_hours": 1}


def get_auto_backup_config() -> dict:
    """Get the current auto-backup configuration."""
    if os.path.exists(AUTO_BACKUP_CONFIG_FILE):
        try:
            with open(AUTO_BACKUP_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            backup_logger.error(f"Error reading auto-backup config: {e}")
    return DEFAULT_AUTO_BACKUP_CONFIG.copy()


def save_auto_backup_config(config: dict):
    """Save the auto-backup configuration."""
    os.makedirs(os.path.dirname(AUTO_BACKUP_CONFIG_FILE), exist_ok=True)
    atomic_write_json(AUTO_BACKUP_CONFIG_FILE, config)


def create_auto_backup_keyboard(config: dict) -> InlineKeyboardMarkup:
    """Create the auto-backup settings menu keyboard."""
    enabled = config.get("enabled", True)
    interval = config.get("interval_hours", 1)

    toggle_text = "🔴 Disable Auto-Backup" if enabled else "🟢 Enable Auto-Backup"

    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data="auto_backup_toggle")],
    ]

    if enabled:
        # Interval options with checkmarks
        intervals = [
            (1, "1h", "auto_backup_interval_1h"),
            (3, "3h", "auto_backup_interval_3h"),
            (6, "6h", "auto_backup_interval_6h"),
            (12, "12h", "auto_backup_interval_12h"),
        ]

        interval_buttons = []
        for hours, label, callback in intervals:
            check = "✅ " if interval == hours else ""
            interval_buttons.append(InlineKeyboardButton(f"{check}{label}", callback_data=callback))

        # Split into rows of 2
        keyboard.append(interval_buttons[:2])
        keyboard.append(interval_buttons[2:])

        # Backup now button
        keyboard.append([InlineKeyboardButton("📦 Backup Now", callback_data="auto_backup_now")])

    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="settings_menu")])

    return InlineKeyboardMarkup(keyboard)


async def auto_backup_menu(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Show the auto-backup settings menu."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    async with _auto_backup_lock:
        config = await asyncio.to_thread(get_auto_backup_config)
    enabled = config.get("enabled", True)
    interval = config.get("interval_hours", 1)

    status_icon = "✅" if enabled else "❌"
    status_text = "Enabled" if enabled else "Disabled"

    message = (
        f"💾 <b>Auto-Backup Settings</b>\n\n"
        f"Status: {status_icon} <code>{status_text}</code>\n"
        f"Interval: <code>{interval} hour(s)</code>\n\n"
        f"💡 <i>Automatic backups are sent to all admins\n"
        f"containing config, database, and legacy files.</i>"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text=message, parse_mode="HTML", reply_markup=create_auto_backup_keyboard(config)
        )
    else:
        await update.message.reply_html(text=message, reply_markup=create_auto_backup_keyboard(config))

    return ConversationHandler.END


async def auto_backup_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-backup on/off."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    async with _auto_backup_lock:
        config = await asyncio.to_thread(get_auto_backup_config)
        config["enabled"] = not config.get("enabled", True)
        await asyncio.to_thread(save_auto_backup_config, config)

    # Reschedule the job
    await reschedule_auto_backup(context.application)

    return await auto_backup_menu(update, context)


async def auto_backup_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, hours: int):
    """Set the auto-backup interval."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    async with _auto_backup_lock:
        config = await asyncio.to_thread(get_auto_backup_config)
        config["interval_hours"] = hours
        await asyncio.to_thread(save_auto_backup_config, config)

    # Reschedule the job
    await reschedule_auto_backup(context.application)

    if update.callback_query:
        await update.callback_query.answer(f"✅ Interval set to {hours} hour(s)")

    return await auto_backup_menu(update, context)


async def auto_backup_now(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Trigger an immediate backup."""
    check = await check_admin_privilege(update)
    if check is not None:
        return check

    if update.callback_query:
        await update.callback_query.answer("📦 Creating backup...")

    # Run the backup
    sent = await send_automatic_backup()

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=("✅ <b>Backup sent successfully!</b>" if sent else "❌ <b>Backup failed. Check the service log.</b>"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="auto_backup_menu")]]),
        )

    return ConversationHandler.END


async def reschedule_auto_backup(application):
    """Reschedule the auto-backup job based on current config."""
    job_queue = application.job_queue
    if not job_queue:
        backup_logger.warning("Job queue not available")
        return

    # Remove existing job
    current_jobs = job_queue.get_jobs_by_name("automatic_backup")
    for job in current_jobs:
        job.schedule_removal()

    async with _auto_backup_lock:
        config = await asyncio.to_thread(get_auto_backup_config)
    if not config.get("enabled", True):
        backup_logger.info("Auto-backup disabled, job removed")
        return

    interval_hours = config.get("interval_hours", 1)

    async def _auto_backup_job(context):
        await send_automatic_backup()

    job_queue.run_repeating(
        _auto_backup_job,
        interval=timedelta(hours=interval_hours),
        first=timedelta(hours=interval_hours),
        name="automatic_backup",
    )
    backup_logger.info(f"✓ Auto-backup scheduled (every {interval_hours} hour(s))")


# Callback handlers for interval settings
async def handle_auto_backup_interval_1h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await auto_backup_set_interval(update, context, 1)


async def handle_auto_backup_interval_3h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await auto_backup_set_interval(update, context, 3)


async def handle_auto_backup_interval_6h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await auto_backup_set_interval(update, context, 6)


async def handle_auto_backup_interval_12h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await auto_backup_set_interval(update, context, 12)
