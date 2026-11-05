"""Atomic one-time conversion of scalar bot messages to explicit step lists."""

import json
import sqlite3
import subprocess

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_mirza_persistence import alembic

BOT = "10000000-0000-4000-8000-000000000001"
OTHER = "20000000-0000-4000-8000-000000000002"


def legacy_database(path, steps=None):
    alembic(path, "upgrade", "013_notification_bots")
    with sqlite3.connect(path) as db:
        if steps is not None:
            # ConfigCRUD stores the JSON-string setting in a JSON column.
            db.execute(
                "INSERT INTO config (key, value) VALUES ('punishment_steps', ?)", (json.dumps(json.dumps(steps)),)
            )
        db.execute(
            "INSERT INTO mirza_routes (id, name, base_url, api_key, message_text, enabled, trigger_mode, revision) VALUES (?, 'Sales', 'https://bot.example/api', 'synthetic-key', 'Last [Username] [Limit]', 1, 'last', 7)",
            (BOT,),
        )
        db.execute(
            "INSERT INTO mirza_routes (id, name, message_text, enabled, trigger_mode, selected_steps, revision) VALUES (?, 'Draft', '', 0, 'selected', '[0, 2, 8]', 8)",
            (OTHER,),
        )
        db.executemany(
            "INSERT INTO mirza_route_admins (admin_username, route_id) VALUES (?, ?)",
            [("owner", BOT), ("other", OTHER)],
        )
        context = json.dumps(
            {
                "candidates": [{"route_id": BOT, "revision": 7, "admin_username": "owner"}],
                "step_index": 2,
                "step_count": 3,
                "step_type": "revoke",
                "effective_limit": 2,
            }
        )
        for state in ("pending", "ready", "sending", "retry", "delivered", "failed", "skipped", "uncertain"):
            db.execute(
                "INSERT INTO mirza_notifications (incident_id, username, applied_at, context, state, route_id, route_revision, message_text, attempts) VALUES (?, 'customer', 100, ?, ?, ?, 7, 'Pinned', 2)",
                (state, context, state, BOT),
            )


async def bootstrap(path, monkeypatch):
    import db.database as database

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "DB_AVAILABLE", True)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    try:
        await database.init_db()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bootstrap_first", [False, True])
async def test_populated_013_conversion_preserves_bots_receipts_and_deleted_all_restamps(
    tmp_path, monkeypatch, bootstrap_first
):
    path = tmp_path / "upgrade.db"
    legacy_database(path, [{"type": "warning"}, {"type": "disable", "duration": 10}, {"type": "revoke"}])
    with sqlite3.connect(path) as db:
        old_columns = [row[1] for row in db.execute("PRAGMA table_info(mirza_routes)")]
        columns_sql = ", ".join(old_columns)
        bots_before = db.execute("SELECT " + columns_sql + " FROM mirza_routes ORDER BY id").fetchall()
        receipts = db.execute("SELECT * FROM mirza_notifications ORDER BY incident_id").fetchall()
        owners = db.execute("SELECT * FROM mirza_route_admins ORDER BY admin_username").fetchall()
    if bootstrap_first:
        await bootstrap(path, monkeypatch)
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT " + columns_sql + " FROM mirza_routes ORDER BY id").fetchall() == bots_before
        assert db.execute("SELECT * FROM mirza_notifications ORDER BY incident_id").fetchall() == receipts
        assert db.execute("SELECT * FROM mirza_route_admins ORDER BY admin_username").fetchall() == owners
        entries = {key: json.loads(value) for key, value in db.execute("SELECT id, messages FROM mirza_routes")}
        assert entries[BOT] == [
            {"id": BOT, "name": "Default message", "message_text": "Last [Username] [Limit]", "selected_steps": [2]}
        ]
        assert entries[OTHER] == [
            {"id": OTHER, "name": "Default message", "message_text": "", "selected_steps": [0, 2, 8]}
        ]
        db.execute("UPDATE mirza_routes SET messages = '[]'")
    alembic(path, "stamp", "013_notification_bots")
    alembic(path, "upgrade", "head")
    await bootstrap(path, monkeypatch)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT messages FROM mirza_routes").fetchall() == [("[]",), ("[]",)]
        assert db.execute("SELECT * FROM mirza_notifications ORDER BY incident_id").fetchall() == receipts


@pytest.mark.parametrize(
    "steps,expected",
    [
        (None, [4]),
        ([], [4]),
        ([None, "bad"], [4]),
        ([None, {"type": "disable"}, {"type": "unknown"}], [1]),
        ([{"type": "warning"}], [0]),
        ([{"type": "disable"}] * 11, []),
    ],
)
def test_legacy_dynamic_last_is_frozen_once_using_effective_ladder(tmp_path, steps, expected):
    path = tmp_path / "steps.db"
    legacy_database(path, steps)
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert (
            json.loads(db.execute("SELECT messages FROM mirza_routes WHERE id=?", (BOT,)).fetchone()[0])[0][
                "selected_steps"
            ]
            == expected
        )
        db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('punishment_steps', ?)",
            (json.dumps(json.dumps([{"type": "revoke"}])),),
        )
    alembic(path, "stamp", "013_notification_bots")
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert (
            json.loads(db.execute("SELECT messages FROM mirza_routes WHERE id=?", (BOT,)).fetchone()[0])[0][
                "selected_steps"
            ]
            == expected
        )


@pytest.mark.parametrize("use_bootstrap", [False, True])
async def test_failed_backfill_rolls_back_added_column_and_all_data(tmp_path, monkeypatch, use_bootstrap):
    path = tmp_path / "failed.db"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER fail_message_copy BEFORE UPDATE ON mirza_routes BEGIN SELECT RAISE(ABORT, 'synthetic backfill failure'); END"
        )
    if use_bootstrap:
        with pytest.raises(Exception, match="synthetic backfill failure"):
            await bootstrap(path, monkeypatch)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert "messages" not in {row[1] for row in db.execute("PRAGMA table_info(mirza_routes)")}
        assert db.execute("SELECT id, revision FROM mirza_routes ORDER BY id").fetchall() == [(BOT, 7), (OTHER, 8)]
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("013_notification_bots",)
        assert db.execute("SELECT COUNT(*) FROM mirza_notifications").fetchone() == (8,)


def test_014_downgrade_refuses_without_any_mutation(tmp_path):
    path = tmp_path / "downgrade.db"
    legacy_database(path)
    alembic(path, "upgrade", "head")
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    with pytest.raises(subprocess.CalledProcessError) as failure:
        alembic(path, "downgrade", "013_notification_bots")
    assert "full pre-upgrade backup" in failure.value.stderr
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
