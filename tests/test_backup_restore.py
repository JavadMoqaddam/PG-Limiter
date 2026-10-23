"""Backups preserve committed WAL data and restore only complete verified input."""

import io
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest


STATE_ROLES = (
    "warnings",
    "trust_history",
    "violation_history",
    "user_groups",
    "topics",
    "topic_message_cache",
    "pending_notifications",
    "disable_messages",
    "auto_backup_config",
    "legacy_disabled_users",
    "legacy_config",
)


@pytest.fixture
def layout(tmp_path):
    from utils.backup import BackupLayout

    db = tmp_path / "custom" / "configured.sqlite3"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE committed (value TEXT)")
        connection.execute("INSERT INTO committed VALUES ('original')")
    env = tmp_path / "config" / ".env"
    env.parent.mkdir()
    env.write_text(f"DATABASE_URL=sqlite+aiosqlite:///{db}\nBOT_TOKEN=secret\n")
    states = {role: tmp_path / "state" / f"{role}.json" for role in STATE_ROLES}
    return BackupLayout(f"sqlite+aiosqlite:///{db}", env, states, allowed_database_roots=(tmp_path,))


def test_existing_bot_backup_snapshots_actual_custom_wal_database(tmp_path, monkeypatch):
    import db.database as database
    import telegram_bot.handlers.backup as handler

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "custom.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE committed (value TEXT)")
    connection.execute("INSERT INTO committed VALUES ('committed while open')")
    connection.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda p: False if str(p).startswith(("/var/lib/pg-limiter", "/etc/opt/pg-limiter")) else exists(p),
    )
    try:
        archive, _, temp_dir = handler._sync_create_backup_zip()
        with zipfile.ZipFile(archive) as backup:
            assert "database.sqlite3" in backup.namelist()
            assert not any(name.endswith(("-wal", "-shm")) for name in backup.namelist())
            restored = tmp_path / "snapshot.sqlite3"
            restored.write_bytes(backup.read("database.sqlite3"))
        with sqlite3.connect(restored) as snapshot:
            assert snapshot.execute("SELECT value FROM committed").fetchall() == [("committed while open",)]
    finally:
        connection.close()


def test_missing_database_cannot_report_backup_success(layout, tmp_path):
    from utils.backup import create_backup

    Path(layout.database_url.split("///", 1)[1]).unlink()
    with pytest.raises(ValueError, match="database.*missing|database.*exist"):
        create_backup(tmp_path / "backup.zip", layout)
    assert not (tmp_path / "backup.zip").exists()


def test_backup_records_all_actual_state_and_explicit_absence(layout, tmp_path):
    from utils.backup import create_backup

    for role, path in layout.state_paths.items():
        path.parent.mkdir(exist_ok=True)
        if role != "topics":
            path.write_text(json.dumps({role: "saved"}))
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    with zipfile.ZipFile(archive) as backup:
        names = backup.namelist()
        assert names.count("config/.env") == 1
        manifest = json.loads(backup.read("manifest.json"))
        assert set(manifest["states"]) == set(STATE_ROLES)
        assert manifest["states"]["topics"]["present"] is False
        for role in set(STATE_ROLES) - {"topics"}:
            assert json.loads(backup.read(f"state/{role}.json")) == {role: "saved"}
        assert "SQLite backup API" in backup.read("backup_info.txt").decode()


def test_roundtrip_restores_configured_database_and_removes_offline_sidecars(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    database = Path(layout.database_url.split("///", 1)[1])
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).write_bytes(b"stale sidecar")
    with pytest.raises(ValueError, match="stopped|offline"):
        apply_backup(archive, layout)
    assert Path(str(database) + "-wal").exists()
    apply_backup(archive, layout, service_stopped=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_restored_environment_selects_next_lifecycle_database(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    original = Path(layout.database_url.split("///", 1)[1])
    destination = tmp_path / "other" / "next.sqlite3"
    layout.env_path.write_text(f"DATABASE_URL=sqlite+aiosqlite:///{destination}\n")
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    layout.env_path.write_text(f"DATABASE_URL=sqlite+aiosqlite:///{original}\n")
    apply_backup(archive, layout, service_stopped=True)
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]
    assert str(destination) in layout.env_path.read_text()


@pytest.mark.parametrize("member", ["../escape", "/absolute", "data/../escape", "config\\..\\escape"])
def test_malicious_archive_leaves_all_targets_unchanged(layout, tmp_path, member):
    from utils.backup import apply_backup

    database = Path(layout.database_url.split("///", 1)[1])
    before = database.read_bytes(), layout.env_path.read_bytes()
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr(member, "evil")
    with pytest.raises(ValueError):
        apply_backup(archive, layout, service_stopped=True)
    assert (database.read_bytes(), layout.env_path.read_bytes()) == before


def test_partial_archive_and_corrupt_database_never_mutate_targets(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    database = Path(layout.database_url.split("///", 1)[1])
    before = database.read_bytes(), layout.env_path.read_bytes()
    archive = tmp_path / "complete.zip"
    create_backup(archive, layout)
    for mode in ("missing", "corrupt"):
        damaged = tmp_path / f"{mode}.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(damaged, "w") as target:
            for name in source.namelist():
                if name == "database.sqlite3":
                    if mode == "corrupt":
                        target.writestr(name, b"not SQLite")
                    continue
                target.writestr(name, source.read(name))
        with pytest.raises(ValueError):
            apply_backup(damaged, layout, service_stopped=True)
        assert (database.read_bytes(), layout.env_path.read_bytes()) == before


def test_staging_verifies_input_without_touching_live_state(layout, tmp_path):
    from utils.backup import create_backup, stage_backup

    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    database = Path(layout.database_url.split("///", 1)[1])
    before = database.read_bytes(), layout.env_path.read_bytes()
    stage_dir = tmp_path / "restore-staging"
    staged = stage_backup(archive.read_bytes(), layout, stage_dir)
    assert staged.parent == stage_dir
    assert staged.read_bytes() == archive.read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert stat.S_IMODE(stage_dir.stat().st_mode) == 0o700
    assert (database.read_bytes(), layout.env_path.read_bytes()) == before
    with pytest.raises(ValueError):
        stage_backup(b"invalid", layout, stage_dir)
    assert list(stage_dir.iterdir()) == [staged]


@pytest.mark.parametrize("db_member", ["data/pg_limiter.db", "data/data/pg_limiter.db"])
def test_legacy_bot_and_host_archives_restore_database_truthfully(layout, tmp_path, db_member):
    from utils.backup import apply_backup

    database = Path(layout.database_url.split("///", 1)[1])
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.write(database, db_member)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    result = apply_backup(archive, layout, service_stopped=True)
    assert result.kind == "database"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]


def test_legacy_archive_without_database_is_rejected(layout, tmp_path):
    from utils.backup import apply_backup

    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr("config/.env", "BOT_TOKEN=archived")
    original = layout.env_path.read_bytes()
    with pytest.raises(ValueError, match="database"):
        apply_backup(archive, layout, service_stopped=True)
    assert layout.env_path.read_bytes() == original


def test_existing_bot_restore_rejects_database_missing_archive_without_live_config_change(tmp_path, monkeypatch):
    import telegram_bot.handlers.backup as handler

    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=live")
    exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if str(p).startswith("/etc/opt/pg-limiter") else exists(p))
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("config/.env", "BOT_TOKEN=archived")
    with pytest.raises(ValueError, match="database"):
        handler._sync_restore_config_zip(content.getvalue())
    assert env.read_text() == "BOT_TOKEN=live"


async def test_group_records_survive_fresh_instances_and_preserve_legacy_copy(tmp_path, monkeypatch):
    from utils.user_groups_storage import UserGroupsStorage

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PG_LIMITER_DATA_DIR", str(tmp_path / "persistent"))
    legacy = tmp_path / ".user_groups_backup.json"
    legacy.write_text('{"user_groups":{"alice":{"groups":[3],"saved_at":10}}}')
    original = legacy.read_bytes()
    store = UserGroupsStorage()
    assert await store.get_user_groups("alice") == [3]
    await store.save_user_groups("bob", [4])
    assert legacy.read_bytes() == original
    assert Path(store.filename) == tmp_path / "persistent" / "user_groups_backup.json"
    legacy.unlink()
    fresh = UserGroupsStorage()
    assert await fresh.get_user_groups("alice") == [3]
    assert await fresh.get_user_groups("bob") == [4]
    legacy.write_bytes(original)
    assert legacy.read_bytes() == original


def test_explicit_absent_state_removes_newer_json_after_offline_restore(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    for path in layout.state_paths.values():
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"newer":"state"}')
    apply_backup(archive, layout, service_stopped=True)
    assert not any(path.exists() for role, path in layout.state_paths.items() if role != "user_groups")
    assert json.loads(layout.state_paths["user_groups"].read_text()) == {"user_groups": {}}


async def test_absent_groups_restore_cannot_revive_later_legacy_records(layout, tmp_path, monkeypatch):
    from utils.backup import apply_backup, create_backup
    from utils.backup_paths import groups_file
    from utils.user_groups_storage import UserGroupsStorage

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PG_LIMITER_DATA_DIR", str(tmp_path / "persistent"))
    layout.state_paths["user_groups"] = groups_file()
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    legacy = tmp_path / ".user_groups_backup.json"
    legacy.write_text('{"user_groups":{"alice":{"groups":[3],"saved_at":10}}}')
    original = legacy.read_bytes()
    apply_backup(archive, layout, service_stopped=True)
    fresh = UserGroupsStorage()
    assert await fresh.get_user_groups("alice") is None
    assert legacy.read_bytes() == original


@pytest.mark.parametrize("present", [True, False])
async def test_restored_data_directory_routes_group_state_for_fresh_store(layout, tmp_path, monkeypatch, present):
    from utils.backup import apply_backup, create_backup
    from utils.user_groups_storage import UserGroupsStorage

    current, restored = tmp_path / "current-data", tmp_path / "restored-data"
    current.mkdir()
    restored.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PG_LIMITER_DATA_DIR", str(current))
    layout.state_paths["user_groups"] = current / "user_groups_backup.json"
    layout.state_paths["legacy_config"] = current / "config.json"
    layout.env_path.write_text(f"DATABASE_URL={layout.database_url}\nPG_LIMITER_DATA_DIR={restored}\n")
    if present:
        layout.state_paths["user_groups"].write_text('{"user_groups":{"alice":{"groups":[3]}}}')
        layout.state_paths["legacy_config"].write_text('{"disable_method":"group"}')
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    layout.state_paths["user_groups"].write_text('{"user_groups":{"alice":{"groups":[7]}}}')
    current_bytes = layout.state_paths["user_groups"].read_bytes()
    (restored / "user_groups_backup.json").write_text('{"user_groups":{"alice":{"groups":[8]}}}')
    (restored / "config.json").write_text('{"disable_method":"stale"}')
    apply_backup(archive, layout, service_stopped=True)
    monkeypatch.setenv("PG_LIMITER_DATA_DIR", str(restored))
    fresh = UserGroupsStorage()
    assert await fresh.get_user_groups("alice") == ([3] if present else None)
    assert layout.state_paths["user_groups"].read_bytes() == current_bytes
    if present:
        assert json.loads((restored / "config.json").read_text()) == {"disable_method": "group"}
    else:
        assert not (restored / "config.json").exists()


def test_restored_data_directory_is_validated_against_persistent_roots(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    current, ephemeral = tmp_path / "persistent-state", tmp_path / "ephemeral-state"
    current.mkdir()
    layout.state_paths = {role: current / path.name for role, path in layout.state_paths.items()}
    layout.env_path.write_text(f"DATABASE_URL={layout.database_url}\nPG_LIMITER_DATA_DIR={ephemeral}\n")
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    layout.persistent_state_roots = (current, tmp_path / "custom")
    original_env = layout.env_path.read_bytes()
    with pytest.raises(ValueError, match="persistent"):
        apply_backup(archive, layout, service_stopped=True)
    assert layout.env_path.read_bytes() == original_env
    assert not ephemeral.exists()


@pytest.mark.parametrize("kind", ["symlink", "duplicate", "duplicate_directory"])
def test_symlink_and_duplicate_members_are_rejected(layout, tmp_path, kind):
    from utils.backup import apply_backup

    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        entry = zipfile.ZipInfo("data/pg_limiter.db")
        if kind == "symlink":
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../live")
        elif kind == "duplicate":
            archive.writestr(entry, "first")
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("data/pg_limiter.db", "second")
        else:
            archive.writestr("data/", "")
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("data/", "")
            archive.write(Path(layout.database_url.split("///", 1)[1]), "data/pg_limiter.db")
    before = layout.env_path.read_bytes()
    with pytest.raises(ValueError, match="Unsafe|duplicate"):
        apply_backup(path, layout, service_stopped=True)
    assert layout.env_path.read_bytes() == before


def test_application_failure_rolls_back_preceding_database_replacement(layout, tmp_path, monkeypatch):
    import utils.backup as backup

    archive = tmp_path / "backup.zip"
    state = layout.state_paths["warnings"]
    state.parent.mkdir()
    state.write_text('{"version":"archived"}')
    backup.create_backup(archive, layout)
    database = Path(layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    state.write_text('{"version":"live"}')
    original = database.read_bytes(), state.read_bytes(), layout.env_path.read_bytes()
    replace = os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if Path(target) == state and not failed:
            failed = True
            raise OSError("simulated state replacement failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_once)
    with pytest.raises(OSError, match="simulated"):
        backup.apply_backup(archive, layout, service_stopped=True)
    assert (database.read_bytes(), state.read_bytes(), layout.env_path.read_bytes()) == original


@pytest.mark.parametrize("failure", ["replace", "bound_copy"])
def test_incomplete_rollback_recovers_other_targets_and_retains_original(layout, tmp_path, monkeypatch, failure):
    import errno
    import utils.backup as backup

    state = layout.state_paths["warnings"]
    state.parent.mkdir()
    state.write_text('{"version":"archived"}')
    archive = tmp_path / "backup.zip"
    backup.create_backup(archive, layout)
    database = Path(layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    state.write_text('{"version":"live"}')
    original_database, original_state = database.read_bytes(), state.read_bytes()
    replace, copy = os.replace, backup.shutil.copyfile

    def fail_replace(source, target):
        if Path(target) == state:
            code = errno.EBUSY if failure == "bound_copy" else errno.EROFS
            raise OSError(code, "persistent state replacement failure")
        return replace(source, target)

    def partial_copy(source, target):
        if Path(target) == state:
            state.write_bytes(b"partial copy")
            raise OSError(errno.EROFS, "persistent state copy failure")
        return copy(source, target)

    monkeypatch.setattr(os, "replace", fail_replace)
    if failure == "bound_copy":
        monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == state)
        monkeypatch.setattr(backup.shutil, "copyfile", partial_copy)
    with pytest.raises(OSError) as caught:
        backup.apply_backup(archive, layout, service_stopped=True)
    assert database.read_bytes() == original_database
    recovery = list(state.parent.glob(".restore-old-*"))
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == original_state
    assert str(state) in str(caught.value)
    assert str(recovery[0]) in str(caught.value)


def test_legacy_wal_is_materialized_into_a_single_checked_database(layout, tmp_path):
    from utils.backup import apply_backup

    database = Path(layout.database_url.split("///", 1)[1])
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO committed VALUES ('legacy WAL commit')")
    connection.commit()
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.write(database, "data/configured.sqlite3")
        target.write(str(database) + "-wal", "data/configured.sqlite3-wal")
    connection.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM committed")
    apply_backup(archive, layout, service_stopped=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",), ("legacy WAL commit",)]


def test_file_bound_database_is_replaced_only_offline(layout, tmp_path, monkeypatch):
    import errno
    from utils.backup import apply_backup, create_backup

    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    database = Path(layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    replace = os.replace

    def mounted_replace(source, target):
        if Path(target) == database:
            raise OSError(errno.EBUSY, "bind mount")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", mounted_replace)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == database)
    apply_backup(archive, layout, service_stopped=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]


def test_container_database_destinations_require_real_persistent_mounts(monkeypatch):
    from utils.backup import _container_database_roots

    mounts = "\n".join(
        [
            "10 1 0:1 / / rw - overlay overlay rw",
            "11 10 8:1 /host/data /var/lib/pg-limiter rw - ext4 /dev/vda rw",
            "12 10 8:1 /host/config /etc/opt/pg-limiter rw - ext4 /dev/vda rw",
            "13 10 8:1 /host/db /srv/custom.sqlite3 rw - ext4 /dev/vda rw",
            "14 10 0:2 / /proc rw - proc proc rw",
            "15 10 8:1 /host/hosts /etc/hosts rw - ext4 /dev/vda rw",
        ]
    )
    monkeypatch.setattr(Path, "is_dir", lambda path: path == Path("/var/lib/pg-limiter"))
    assert _container_database_roots(mounts, Path("/srv/custom.sqlite3")) == (
        Path("/var/lib/pg-limiter"),
        Path("/srv/custom.sqlite3"),
    )


def test_database_integrity_is_checked_even_with_matching_manifest_checksum(layout, tmp_path):
    import hashlib
    from utils.backup import apply_backup, create_backup

    original = tmp_path / "original.zip"
    damaged = tmp_path / "damaged.zip"
    create_backup(original, layout)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, "w") as target:
        bad_database = source.read("database.sqlite3")[:100]
        manifest = json.loads(source.read("manifest.json"))
        manifest["database"]["sha256"] = hashlib.sha256(bad_database).hexdigest()
        for name in source.namelist():
            payload = source.read(name)
            if name == "database.sqlite3":
                payload = bad_database
            elif name == "manifest.json":
                payload = json.dumps(manifest).encode()
            target.writestr(name, payload)
    database = Path(layout.database_url.split("///", 1)[1])
    before = database.read_bytes()
    with pytest.raises(ValueError):
        apply_backup(damaged, layout, service_stopped=True)
    assert database.read_bytes() == before


def test_sqlalchemy_connection_options_do_not_change_database_snapshot_path(layout, tmp_path):
    from utils.backup import create_backup

    layout.database_url += "?timeout=10"
    archive = tmp_path / "backup.zip"
    create_backup(archive, layout)
    with zipfile.ZipFile(archive) as source:
        path = tmp_path / "checked.sqlite3"
        path.write_bytes(source.read("database.sqlite3"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]


def test_ephemeral_json_destination_is_rejected_before_any_offline_mutation(layout, tmp_path):
    from utils.backup import apply_backup, create_backup

    archive = tmp_path / "backup.zip"
    state = layout.state_paths["user_groups"]
    state.parent.mkdir()
    state.write_text('{"groups":"archived"}')
    create_backup(archive, layout)
    state.write_text('{"groups":"live"}')
    original = state.read_bytes()
    layout.persistent_state_roots = (tmp_path / "custom",)
    with pytest.raises(ValueError, match="persistent.*state|state.*persistent"):
        apply_backup(archive, layout, service_stopped=True)
    assert state.read_bytes() == original
