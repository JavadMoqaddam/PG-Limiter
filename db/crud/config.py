"""
Config CRUD operations (key-value store).
"""

from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Config
from db.crud.config_revision import ConfigRevisionCRUD, REVISION_KEY
from db.crud.legacy_import import LEGACY_TABLE_IMPORT_KEY
from utils.logs import get_logger

db_config_logger = get_logger("db.config")
_INTERNAL_CONFIG_KEYS = (REVISION_KEY, LEGACY_TABLE_IMPORT_KEY)


class ConfigCRUD:
    """CRUD operations for Config table (key-value store)."""

    @staticmethod
    async def set(db: AsyncSession, key: str, value, *, expected_db_revision: int | None = None) -> Config | None:
        """Set a config value."""
        if key in _INTERNAL_CONFIG_KEYS:
            raise ValueError("The configuration key is internal")
        if await ConfigRevisionCRUD.advance(db, expected_revision=expected_db_revision) is None:
            return None
        db_config_logger.debug(f"📝 Setting config: {key}")
        result = await db.execute(select(Config).where(Config.key == key))
        config = result.scalar_one_or_none()

        if config:
            db_config_logger.debug(f"✏️ Updating config: {key}")
            config.value = value
            config.updated_at = datetime.now(timezone.utc)
        else:
            db_config_logger.debug(f"➕ Creating config: {key}")
            config = Config(key=key, value=value)
            db.add(config)

        await db.flush()
        db_config_logger.debug(f"✅ Config {key} set")
        return config

    @staticmethod
    async def get(db: AsyncSession, key: str, default=None):
        """Get a config value."""
        db_config_logger.debug(f"🔍 Getting config: {key}")
        result = await db.execute(select(Config).where(Config.key == key))
        config = result.scalar_one_or_none()
        return config.value if config else default

    @staticmethod
    async def delete(db: AsyncSession, key: str) -> bool:
        """Delete a config key."""
        if key in _INTERNAL_CONFIG_KEYS:
            raise ValueError("The configuration key is internal")
        db_config_logger.debug(f"🗑️ Deleting config: {key}")
        result = await db.execute(delete(Config).where(Config.key == key))
        deleted = result.rowcount > 0
        if deleted:
            await ConfigRevisionCRUD.advance(db)
            db_config_logger.info(f"✅ Deleted config: {key}")
        return deleted

    @staticmethod
    async def get_all(db: AsyncSession) -> dict:
        """Get all config as a dictionary."""
        db_config_logger.debug("📋 Getting all config")
        result = await db.execute(select(Config).where(Config.key.not_in(_INTERNAL_CONFIG_KEYS)))
        configs = result.scalars().all()
        db_config_logger.debug(f"✅ Retrieved {len(configs)} config entries")
        return {c.key: c.value for c in configs}
