"""Transaction-owned completion of the retained deprecated-table import."""

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from db.models import Config


LEGACY_TABLE_IMPORT_KEY = "_deprecated_legacy_tables_import"
LEGACY_TABLE_IMPORT_VERSION = 1


class LegacyTableImportCRUD:
    """Serialize importers through an internal Config row in their transaction."""

    @staticmethod
    def claim_connection(connection: Connection) -> bool:
        """Lock the lifecycle and return whether this transaction should import.

        The seed write precedes all source reads. SQLite acquires its writer lock;
        other supported dialects retain a row lock through the caller's commit.
        A failed import rolls the seed back alongside every imported record.
        """
        dialect = connection.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert
        else:
            raise RuntimeError(f"Legacy table imports are unsupported for {dialect}")

        seed = insert(Config).values(
            key=LEGACY_TABLE_IMPORT_KEY, value={"version": LEGACY_TABLE_IMPORT_VERSION, "completed": False}
        )
        if dialect in ("mysql", "mariadb"):
            seed = seed.on_duplicate_key_update(key=Config.key)
        else:
            seed = seed.on_conflict_do_nothing(index_elements=[Config.key])
        connection.execute(seed)
        value = connection.execute(
            select(Config.value).where(Config.key == LEGACY_TABLE_IMPORT_KEY).with_for_update()
        ).scalar_one()
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != LEGACY_TABLE_IMPORT_VERSION
            or type(value.get("completed")) is not bool
        ):
            raise ValueError("Invalid legacy table import completion marker")
        return not value["completed"]

    @staticmethod
    def complete_connection(connection: Connection) -> None:
        """Complete only after all source writes and policy versioning succeed."""
        connection.execute(
            update(Config)
            .where(Config.key == LEGACY_TABLE_IMPORT_KEY)
            .values(
                value={"version": LEGACY_TABLE_IMPORT_VERSION, "completed": True},
                updated_at=datetime.now(timezone.utc),
            )
        )
