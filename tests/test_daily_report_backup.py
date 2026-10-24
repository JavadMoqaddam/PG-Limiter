"""Backup staging/offline restoration retain daily-report ownership and evidence."""

import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from tests.test_backup_restore import layout  # noqa: F401


def test_runtime_backup_paths_include_actual_owned_outbox(tmp_path, monkeypatch):
    import utils.punishment_facts as facts
    from utils.backup_paths import state_paths

    owned = tmp_path / "owned-outbox.json"
    monkeypatch.setattr(facts, "_journal", facts.PunishmentFactJournal(str(owned)))
    assert state_paths()["punishment_fact_outbox"] == owned


def test_snapshot_stage_and_offline_restore_keep_pending_time_and_known_receipt(layout, tmp_path):
    from sqlalchemy import create_engine
    from db.models import Base
    from utils.backup import apply_backup, create_backup, validate_backup

    outbox = tmp_path / "state" / "punishment_fact_outbox.json"
    outbox.parent.mkdir(exist_ok=True)
    layout.state_paths["punishment_fact_outbox"] = outbox
    pending = {
        "pending": {
            "incident_id": "pending",
            "username": "alice",
            "step_index": 3,
            "started_at": 99.0,
            "applied_at": 100.0,
            "disable_succeeded": True,
            "revoke_succeeded": False,
            "uuid_reset_succeeded": False,
            "state": "completed",
        }
    }
    outbox.write_text(json.dumps(pending))
    database = Path(layout.database_url.split("///", 1)[1])
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO punishment_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("known", "bob", 2, 90.0, 91.0, True, False, False, "completed"),
        )
        db.execute(
            "INSERT INTO daily_report_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-23", "UTC", 90.0, 100.0, "Total: 1 users", "daily:2026-10-23", "delivered", -100123, 9, 0, 0, 77),
        )
    archive = tmp_path / "daily.zip"
    create_backup(archive, layout)
    outbox.write_text('{"newer":"live"}')
    with sqlite3.connect(database) as db:
        db.execute("UPDATE punishment_facts SET applied_at = 999")
        db.execute("UPDATE daily_report_runs SET message_id = 999")
    with validate_backup(archive, layout) as staged:
        assert json.loads(staged.files[outbox.resolve()].read_text()) == pending
        assert json.loads(outbox.read_text()) == {"newer": "live"}
    apply_backup(archive, layout, service_stopped=True)
    assert json.loads(outbox.read_text()) == pending
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT applied_at FROM punishment_facts").fetchone() == (91.0,)
        assert db.execute("SELECT state, message_id FROM daily_report_runs").fetchone() == ("delivered", 77)


def test_v1_backup_remains_valid_and_removes_stale_new_outbox(layout, tmp_path):
    from utils.backup import apply_backup, create_backup, validate_backup

    outbox = tmp_path / "state" / "punishment_fact_outbox.json"
    layout.state_paths["punishment_fact_outbox"] = outbox
    archive = tmp_path / "current.zip"
    create_backup(archive, layout)
    old = tmp_path / "v1.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(old, "w") as target:
        for name in source.namelist():
            if name == "state/punishment_fact_outbox.json":
                continue
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["version"] = 1
                manifest["states"].pop("punishment_fact_outbox")
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    outbox.parent.mkdir(exist_ok=True)
    outbox.write_text('{"stale":"must not replay"}')
    with validate_backup(old, layout) as plan:
        assert plan.files[outbox.resolve()] is None
        assert outbox.exists()
    apply_backup(old, layout, service_stopped=True)
    assert not outbox.exists()


def test_v2_manifest_cannot_omit_outbox_role(layout, tmp_path):
    from utils.backup import create_backup, validate_backup

    layout.state_paths["punishment_fact_outbox"] = tmp_path / "outbox.json"
    archive = tmp_path / "current.zip"
    create_backup(archive, layout)
    bad = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(bad, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["version"] = 2
                manifest["states"].pop("punishment_fact_outbox")
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    with pytest.raises(ValueError, match="Incomplete backup state manifest"):
        with validate_backup(bad, layout):
            pass
