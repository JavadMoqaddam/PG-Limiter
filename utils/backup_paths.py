"""Owned persistence paths shared by runtime stores and backup/restore."""

import os
from pathlib import Path

AUTO_BACKUP_CONFIG_FILE = "data/auto_backup_config.json"


def groups_file(data_dir: str | Path | None = None) -> Path:
    """Persist old group restoration records across fresh container lifecycles."""
    root = data_dir if data_dir is not None else os.environ.get("PG_LIMITER_DATA_DIR", "/var/lib/pg-limiter")
    return Path(root) / "user_groups_backup.json"


def legacy_config_file(data_dir: str | Path | None = None) -> Path:
    root = data_dir if data_dir is not None else os.environ.get("PG_LIMITER_DATA_DIR", "/var/lib/pg-limiter")
    return Path(root) / "config.json"


def legacy_groups_file() -> Path:
    local = Path(".user_groups_backup.json")
    return local if local.is_file() else Path("/app/.user_groups_backup.json")


def state_paths() -> dict[str, Path]:
    """Read the current owners rather than maintaining duplicate path defaults."""
    from utils.warning_system import warning_system
    from utils.punishment_system import punishment_system
    from utils.handel_dis_users import LEGACY_JSON_PATH
    from telegram_bot.topics import TOPICS_FILE, MESSAGE_CACHE_FILE
    from telegram_bot.dispatcher import PENDING_NOTIFICATIONS_FILE
    from telegram_bot.send_message import DISABLE_MESSAGES_FILE
    from telegram_bot.handlers.backup import AUTO_BACKUP_CONFIG_FILE

    return {
        "warnings": Path(warning_system.filename),
        "trust_history": Path(warning_system.history_filename),
        "violation_history": Path(punishment_system.filename),
        "user_groups": groups_file(),
        "topics": Path(TOPICS_FILE),
        "topic_message_cache": Path(MESSAGE_CACHE_FILE),
        "pending_notifications": Path(PENDING_NOTIFICATIONS_FILE),
        "disable_messages": Path(DISABLE_MESSAGES_FILE),
        "auto_backup_config": Path(AUTO_BACKUP_CONFIG_FILE),
        "legacy_disabled_users": Path(LEGACY_JSON_PATH),
        "legacy_config": legacy_config_file(),
    }
