"""One-time addition of independent forum receipt ownership."""

from sqlalchemy import inspect, text


def ensure_notification_report_schema(connection):
    columns = {item["name"] for item in inspect(connection).get_columns("mirza_notifications")}
    definitions = {
        "bot_name": "VARCHAR(255)",
        "report_state": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        "report_chat_id": "INTEGER",
        "report_thread_id": "INTEGER",
        "report_message_id": "INTEGER",
    }
    present = set(definitions) & columns
    if present and present != set(definitions):
        raise RuntimeError("Incomplete notification report schema; restore full pre-upgrade backup")
    if not present:
        if connection.dialect.name == "sqlite":
            connection.execute(text("UPDATE mirza_notifications SET incident_id=incident_id WHERE 1=0"))
        for name, definition in definitions.items():
            connection.execute(text(f"ALTER TABLE mirza_notifications ADD COLUMN {name} {definition}"))
        connection.execute(
            text(
                "UPDATE mirza_notifications SET report_state='historical' "
                "WHERE state IN ('delivered', 'failed', 'skipped', 'uncertain')"
            )
        )
    indexes = {index["name"] for index in inspect(connection).get_indexes("mirza_notifications")}
    if "ix_mirza_notifications_report_state" not in indexes:
        connection.execute(
            text("CREATE INDEX ix_mirza_notifications_report_state ON mirza_notifications (report_state, state)")
        )
