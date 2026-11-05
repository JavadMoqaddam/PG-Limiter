"""One-time 014 conversion shared by Alembic and pre-Alembic database bootstrap."""

import json

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


def _legacy_step_count(connection: Connection) -> int:
    """Match the pre-014 ladder's dict filtering and five-step default once."""
    value = connection.execute(text("SELECT value FROM config WHERE key = 'punishment_steps'")).scalar()
    # Config.value is JSON; the steps setting itself is a serialized JSON string.
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            return 5
    count = sum(isinstance(step, dict) for step in value) if isinstance(value, list) else 0
    return count or 5


def ensure_message_list_schema(connection: Connection) -> None:
    """Add/backfill only if the column is absent; empty lists are authoritative."""
    columns = {column["name"] for column in inspect(connection).get_columns("mirza_routes")}
    if "messages" in columns:
        return
    if connection.dialect.name == "sqlite":
        # Legacy SQLite transaction mode otherwise commits DDL before backfill.
        connection.execute(text("UPDATE mirza_routes SET id = id WHERE 1 = 0"))
    step_count = _legacy_step_count(connection)
    connection.execute(text("ALTER TABLE mirza_routes ADD COLUMN messages JSON NOT NULL DEFAULT '[]'"))
    rows = connection.execute(text("SELECT id, message_text, trigger_mode, selected_steps FROM mirza_routes"))
    for row in rows.mappings():
        if row["trigger_mode"] == "last":
            steps = [step_count - 1] if step_count <= 10 else []
        elif row["trigger_mode"] == "selected":
            steps = row["selected_steps"]
            if isinstance(steps, str):
                steps = json.loads(steps)
        else:
            raise RuntimeError("Cannot migrate an invalid notification trigger mode")
        if not isinstance(steps, list) or any(type(step) is not int or not 0 <= step <= 9 for step in steps):
            raise RuntimeError("Cannot migrate invalid notification step selections")
        messages = [
            {
                "id": row["id"],
                "name": "Default message",
                "message_text": row["message_text"],
                "selected_steps": sorted(set(steps)),
            }
        ]
        connection.execute(
            text("UPDATE mirza_routes SET messages = :messages WHERE id = :id"),
            {"id": row["id"], "messages": json.dumps(messages)},
        )
