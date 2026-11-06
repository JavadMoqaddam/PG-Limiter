"""Upgrade preserves customer evidence and excludes historical forum replay."""

import sqlite3

import pytest

from test_mirza_bot_groups import alembic
from test_mirza_message_migration import bootstrap


@pytest.mark.parametrize("bootstrap_first", [False, True])
async def test_report_migration_is_once_and_preserves_customer_rows(tmp_path, monkeypatch, bootstrap_first):
    path = tmp_path / "reports.db"
    alembic(path, "upgrade", "014_notification_messages")
    with sqlite3.connect(path) as db:
        for state in ("pending", "ready", "sending", "retry", "delivered", "failed", "skipped", "uncertain"):
            db.execute(
                "INSERT INTO mirza_notifications (incident_id, username, applied_at, context, state, attempts) VALUES (?, 'customer', 100, '{}', ?, 2)",
                (state, state),
            )
        columns = ", ".join(row[1] for row in db.execute("PRAGMA table_info(mirza_notifications)"))
        before = db.execute("SELECT " + columns + " FROM mirza_notifications ORDER BY incident_id").fetchall()
    if bootstrap_first:
        await bootstrap(path, monkeypatch)
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT " + columns + " FROM mirza_notifications ORDER BY incident_id").fetchall() == before

        reports = dict(db.execute("SELECT state, report_state FROM mirza_notifications"))
        assert reports == {
            s: "historical" if s in {"delivered", "failed", "skipped", "uncertain"} else "pending" for s in reports
        }
        db.execute("UPDATE mirza_notifications SET report_state='uncertain' WHERE incident_id='pending'")
    alembic(path, "stamp", "014_notification_messages")
    alembic(path, "upgrade", "head")
    await bootstrap(path, monkeypatch)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT report_state FROM mirza_notifications WHERE incident_id='pending'").fetchone() == (
            "uncertain",
        )
        assert db.execute("SELECT " + columns + " FROM mirza_notifications ORDER BY incident_id").fetchall() == before


def test_report_backfill_failure_rolls_back_columns_and_history(tmp_path):
    from sqlalchemy import create_engine, event
    from db.notification_report_schema import ensure_notification_report_schema

    path = tmp_path / "atomic.db"
    alembic(path, "upgrade", "014_notification_messages")
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    engine = create_engine(f"sqlite:///{path}")

    def fail_backfill(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE mirza_notifications SET report_state"):
            raise RuntimeError("injected backfill failure")

    event.listen(engine, "before_cursor_execute", fail_backfill)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            with engine.begin() as connection:
                ensure_notification_report_schema(connection)
    finally:
        engine.dispose()
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
