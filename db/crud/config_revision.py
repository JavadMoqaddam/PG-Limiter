"""Commit-coupled configuration versions shared by independent processes."""

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Config

REVISION_KEY = "_runtime_config_revision"
PENDING_CONFIG_INVALIDATIONS = "pending_config_cache_invalidations"


def register_config_cache_invalidation(db: AsyncSession) -> None:
    """Invalidate the local merged cache only when this transaction commits."""
    session = db.sync_session
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("Configuration invalidation requires an active transaction")
    session.info.setdefault(PENDING_CONFIG_INVALIDATIONS, set()).add(transaction)


def _revision(value) -> int:
    if not isinstance(value, dict):
        raise ValueError("Invalid persisted configuration revision")
    revision = value.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid persisted configuration revision")
    return revision


class ConfigRevisionCRUD:
    """Read or increment the version inside the caller-owned transaction."""

    @staticmethod
    async def read(db: AsyncSession) -> int:
        row = (await db.execute(select(Config.value).where(Config.key == REVISION_KEY))).one_or_none()
        return 0 if row is None else _revision(row[0])

    @staticmethod
    async def advance(db: AsyncSession, *, expected_revision: int | None = None) -> int | None:
        next_revision = await db.run_sync(
            lambda session: ConfigRevisionCRUD._advance(session, session.get_bind().dialect.name, expected_revision)
        )
        if next_revision is not None:
            register_config_cache_invalidation(db)
        return next_revision

    @staticmethod
    def advance_connection(connection: Connection) -> int:
        """Version startup policy imports in their existing connection transaction."""
        return ConfigRevisionCRUD._advance(connection, connection.dialect.name)

    @staticmethod
    def _advance(executor, dialect: str, expected_revision: int | None = None) -> int | None:
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert
        else:
            raise RuntimeError(f"Configuration revision writes are unsupported for {dialect}")

        seed = insert(Config).values(key=REVISION_KEY, value={"revision": 0})
        if dialect in ("mysql", "mariadb"):
            seed = seed.on_duplicate_key_update(key=Config.key)
        else:
            seed = seed.on_conflict_do_nothing(index_elements=[Config.key])
        executor.execute(seed)

        if dialect == "sqlite":
            revision = Config.value["revision"].as_integer()
            statement = update(Config).where(
                Config.key == REVISION_KEY,
                func.json_type(Config.value, "$.revision") == "integer",
                revision >= 0,
            )
            if expected_revision is not None:
                statement = statement.where(revision == expected_revision)
            statement = statement.values(
                value=func.json_object("revision", revision + 1), updated_at=datetime.now(timezone.utc)
            ).returning(Config.value)
            value = executor.execute(statement).scalar_one_or_none()
            if value is None:
                # Distinguish an ordinary superseded write from corrupted state.
                stored = executor.execute(select(Config.value).where(Config.key == REVISION_KEY)).one()
                _revision(stored[0])
                if expected_revision is None:
                    raise ValueError("Invalid persisted configuration revision")
                return None
            next_revision = _revision(value)
        else:
            # The seed/upsert acquires the row lock; retain it through the mutation.
            value = executor.scalar(select(Config.value).where(Config.key == REVISION_KEY).with_for_update())
            current = _revision(value)
            if expected_revision is not None and current != expected_revision:
                return None
            next_revision = current + 1
            executor.execute(
                update(Config)
                .where(Config.key == REVISION_KEY)
                .values(value={"revision": next_revision}, updated_at=datetime.now(timezone.utc))
            )
        return next_revision
