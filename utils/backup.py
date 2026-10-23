"""SQLite-consistent backups and validated restoration for a stopped service."""

import argparse
import asyncio
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy.engine import make_url

from utils.backup_paths import groups_file, legacy_config_file, legacy_groups_file, state_paths

FORMAT = "pg-limiter-backup"
VERSION = 1


def sqlite_path(url: str, cwd: Path | None = None) -> Path:
    """Resolve the actual SQLite URL without creating an engine or database."""
    try:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
            raise ValueError
        if parsed.database.startswith("file:") or parsed.query.get("uri", "false").lower() == "true":
            raise ValueError
        path = Path(parsed.database)
        return (path if path.is_absolute() else (cwd or Path.cwd()) / path).resolve()
    except (ValueError, TypeError) as error:
        raise ValueError("Backup/restore requires a persistent SQLite DATABASE_URL") from error


@dataclass
class BackupLayout:
    database_url: str
    env_path: Path | None
    state_paths: dict[str, Path]
    allowed_database_roots: tuple[Path, ...] = ()
    state_sources: dict[str, Path] = field(default_factory=dict)
    persistent_state_roots: tuple[Path, ...] | None = None

    def state_destinations(self, archived_env: bytes | None) -> dict[str, Path]:
        paths = dict(self.state_paths)
        data_dir = _env_setting(archived_env, "PG_LIMITER_DATA_DIR")
        if data_dir is not None:
            for role, path in {
                "user_groups": groups_file(data_dir),
                "legacy_config": legacy_config_file(data_dir),
            }.items():
                if role in paths:
                    paths[role] = path
        return paths

    def database_target(self, archived_env: bytes | None) -> Path:
        current = sqlite_path(self.database_url)
        target = sqlite_path(_env_database_url(archived_env) or self.database_url)
        roots = self.allowed_database_roots or (current.parent, Path("/var/lib/pg-limiter"))
        if not any(target.is_relative_to(Path(root).resolve()) for root in roots):
            raise ValueError("Restored DATABASE_URL is outside the configured persistent database directories")
        if target in {Path(p).resolve() for p in self.state_destinations(archived_env).values()} or (
            self.env_path is not None and target == self.env_path.resolve()
        ):
            raise ValueError("Restored DATABASE_URL conflicts with a state/configuration file")
        return target


def runtime_layout(*, live_instances: bool = False) -> BackupLayout:
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/pg_limiter.db")
    sqlite_path(url)
    paths = state_paths()
    if live_instances:
        from db.database import DATABASE_URL

        url = DATABASE_URL
    env = Path("/etc/opt/pg-limiter/.env")
    if not env.parent.is_dir():
        env = Path(".env")
    sources = {}
    if not groups_file().is_file() and legacy_groups_file().is_file():
        sources["user_groups"] = legacy_groups_file()
    return BackupLayout(url, env, paths, state_sources=sources)


def _env_setting(content: bytes | None, name: str) -> str | None:
    if content is None:
        return None
    import shlex

    text = content.decode("utf-8")
    result = None
    for line in text.splitlines():
        match = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$", line)
        if match:
            parts = shlex.split(match[1], comments=True)
            if len(parts) != 1 or not parts[0] or "$" in parts[0]:
                raise ValueError(f"Restored {name} must be an explicit nonempty value")
            result = parts[0]
    return result


def _env_database_url(content: bytes | None) -> str | None:
    return _env_setting(content, "DATABASE_URL")


def _check_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Configured SQLite database is missing")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("Backup database is not an initialized SQLite database")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup database integrity check failed")


def _snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ValueError("Configured SQLite database is missing")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=256, sleep=0.05)
    _check_database(destination)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_object(content: bytes) -> dict:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Backup JSON state must be an object")
    return data


def create_backup(destination: Path | str, layout: BackupLayout) -> Path:
    """Capture SQLite atomically; JSON/config files are independent snapshots."""
    destination = Path(destination)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "database.sqlite3"
        _snapshot(sqlite_path(layout.database_url), snapshot)
        content = {}
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "consistency": "SQLite backup API snapshot; JSON/configuration captured independently",
            "database": {"member": "database.sqlite3", "sha256": _file_digest(snapshot)},
            "states": {},
            "environment": {"present": False},
        }
        for role, target in layout.state_paths.items():
            source = layout.state_sources.get(role, target)
            entry = {"present": source.is_file()}
            if entry["present"]:
                payload = source.read_bytes()
                _json_object(payload)
                member = f"state/{role}.json"
                content[member] = payload
                entry.update(member=member, sha256=_digest(payload))
            manifest["states"][role] = entry
        if layout.env_path is not None and layout.env_path.is_file():
            payload = layout.env_path.read_bytes()
            _env_database_url(payload)
            _env_setting(payload, "PG_LIMITER_DATA_DIR")
            content["config/.env"] = payload
            manifest["environment"] = {"present": True, "member": "config/.env", "sha256": _digest(payload)}
        content["manifest.json"] = json.dumps(manifest, indent=2).encode()
        content["backup_info.txt"] = (
            "PG-Limiter Backup\nDatabase: SQLite backup API consistent snapshot.\n"
            "JSON and environment files are captured independently; no cross-store transaction is promised.\n"
            "Upload with /restore to validate/stage; apply offline with pg-limiter restore <file>.\n"
        ).encode()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staged = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "database.sqlite3")
                for name, payload in content.items():
                    archive.writestr(name, payload)
            os.replace(staged, destination)
        finally:
            Path(staged).unlink(missing_ok=True)
    return destination


@dataclass
class RestorePlan:
    kind: str
    files: dict[Path, Path | None]
    database: Path
    config_data: dict | None = None
    notes: tuple[str, ...] = ()


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = {}
    seen = set()
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if (
            not info.filename
            or "\\" in info.filename
            or "\x00" in info.filename
            or path.is_absolute()
            or any(part in (".", "..") for part in info.filename.rstrip("/").split("/"))
            or info.filename in seen
            or stat.S_ISLNK(info.external_attr >> 16)
        ):
            raise ValueError("Unsafe or duplicate backup archive member")
        seen.add(info.filename)
        if not info.is_dir():
            members[info.filename] = info
    return members


def _extract(archive: zipfile.ZipFile, name: str, root: Path) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(name) as source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination)
    return target


def _manifest_plan(archive, members, root: Path, layout: BackupLayout) -> RestorePlan:
    manifest = _json_object(archive.read("manifest.json"))
    if manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest["version"] != VERSION:
        raise ValueError("Unsupported backup manifest format/version")
    states = manifest.get("states")
    if not isinstance(states, dict) or set(states) != set(layout.state_paths):
        raise ValueError("Incomplete backup state manifest")
    expected = {"manifest.json", "backup_info.txt"}

    def checked(entry: dict, expected_name: str, *, database=False) -> Path | None:
        if not isinstance(entry, dict) or type(entry.get("present", True)) is not bool:
            raise ValueError("Invalid backup manifest entry")
        if not entry.get("present", True):
            if set(entry) != {"present"}:
                raise ValueError("Invalid absent-state manifest entry")
            return None
        if entry.get("member") != expected_name or expected_name not in members:
            raise ValueError("Missing or invalid declared backup member")
        extracted = _extract(archive, expected_name, root)
        if entry.get("sha256") != _file_digest(extracted):
            raise ValueError("Backup member checksum failed")
        expected.add(expected_name)
        if not database and expected_name.startswith("state/"):
            _json_object(extracted.read_bytes())
        return extracted

    database = checked(manifest.get("database"), "database.sqlite3", database=True)
    if database is None:
        raise ValueError("Backup database is missing")
    _check_database(database)
    env = checked(manifest.get("environment"), "config/.env")
    env_content = env.read_bytes() if env is not None else None
    target = layout.database_target(env_content)
    destinations = layout.state_destinations(env_content)
    files = {target: database}
    for role, entry in states.items():
        source = checked(entry, f"state/{role}.json")
        if role == "user_groups" and source is None:
            # Preserve logical absence without reviving the retained legacy copy
            # when the fresh process loads its group compatibility store.
            source = root / "empty-user-groups.json"
            source.write_text('{"user_groups":{}}')
        files[destinations[role].resolve()] = source
    if env is not None:
        if layout.env_path is None:
            raise ValueError("No configured environment destination")
        files[layout.env_path.resolve()] = env
    if set(members) != expected:
        raise ValueError("Incomplete backup or undeclared archive members")
    return RestorePlan("database", files, target)


def _legacy_plan(archive, members, root: Path, layout: BackupLayout) -> RestorePlan:
    candidates = [
        name
        for name in members
        if name.startswith("data/") and PurePosixPath(name).suffix in (".db", ".sqlite", ".sqlite3")
    ]
    if len(candidates) != 1:
        raise ValueError("Legacy backup must contain exactly one SQLite database")
    name = candidates[0]
    source = _extract(archive, name, root)
    if name + "-wal" in members:
        _extract(archive, name + "-wal", root)
    snapshot = root / "legacy-snapshot.sqlite3"
    _snapshot(source, snapshot)
    env_name = next((name for name in ("config/.env", ".env") if name in members), None)
    env = _extract(archive, env_name, root) if env_name else None
    env_content = env.read_bytes() if env else None
    target = layout.database_target(env_content)
    files = {target: snapshot}
    if env is not None and layout.env_path is not None:
        files[layout.env_path.resolve()] = env
    for role, path in layout.state_destinations(env_content).items():
        candidates = [f"data/{path.name}", f"data/data/{path.name}"]
        if role == "user_groups":
            candidates.extend(["legacy/.user_groups_backup.json", "data/.user_groups_backup.json"])
        elif role == "violation_history":
            candidates.append("legacy/.violation_history.json")
        elif role == "legacy_disabled_users":
            candidates.append("legacy/.disable_users.json")
        found = [name for name in candidates if name in members]
        if len(found) > 1:
            raise ValueError("Ambiguous legacy state entries")
        if found:
            _json_object(archive.read(found[0]))
            files[path.resolve()] = _extract(archive, found[0], root)
    return RestorePlan(
        "database",
        files,
        target,
        notes=(
            "Legacy archive: original cross-file/database consistency cannot be certified; absent legacy JSON files are left unchanged.",
        ),
    )


def _legacy_config(content: bytes) -> dict:
    data = _json_object(content)
    if not set(data) & {
        "disable_method",
        "disabled_group_id",
        "enhanced_details",
        "limits",
        "except_users",
        "panel",
        "timing",
    }:
        raise ValueError("No supported legacy configuration fields")
    limits = data.get("limits", {})
    special = limits.get("special", {}) if isinstance(limits, dict) else None
    excepted = data.get("except_users", [])
    if not isinstance(special, dict) or any(
        not isinstance(name, str) or not name or (limit is not None and type(limit) is not int)
        for name, limit in special.items()
    ):
        raise ValueError("Invalid legacy special limits")
    if not isinstance(excepted, list) or any(not isinstance(name, str) or not name for name in excepted):
        raise ValueError("Invalid legacy whitelist")
    return data


@contextmanager
def validate_backup(archive_path: Path | str, layout: BackupLayout):
    """Validate/extract privately; no live target is changed by preparation."""
    try:
        with tempfile.TemporaryDirectory(prefix="pg-limiter-restore-") as temporary:
            path, root = Path(archive_path), Path(temporary)
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    members = _safe_members(archive)
                    plan = (
                        _manifest_plan(archive, members, root, layout)
                        if "manifest.json" in members
                        else _legacy_plan(archive, members, root, layout)
                    )
            else:
                data = _legacy_config(path.read_bytes())
                database = layout.database_target(None)
                _check_database(database)
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"config", "users"}.issubset(tables):
                    raise ValueError("Legacy configuration import requires an initialized PG-Limiter database")
                plan = RestorePlan("config", {}, database, config_data=data)
            if any(target.exists() and not target.is_file() for target in plan.files):
                raise ValueError("Restore target is not a regular file")
            if layout.persistent_state_roots is not None:
                env_target = layout.env_path.resolve() if layout.env_path is not None else None
                for target in plan.files:
                    if target != env_target and not any(
                        target.is_relative_to(root) for root in layout.persistent_state_roots
                    ):
                        raise ValueError("Restore state destination is outside persistent mounted storage")
            yield plan
    except (zipfile.BadZipFile, sqlite3.Error, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Invalid or incomplete backup") from error


def stage_backup(content: bytes | bytearray, layout: BackupLayout, stage_directory: Path | str) -> Path:
    """Stage validated uploads in persistent storage without applying them."""
    with tempfile.NamedTemporaryFile() as temporary:
        temporary.write(content)
        temporary.flush()
        with validate_backup(temporary.name, layout) as plan:
            suffix = ".zip" if plan.kind == "database" else ".json"
    directory = Path(stage_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / (uuid.uuid4().hex + suffix)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    return path


async def _import_legacy_config(data: dict, database: Path) -> tuple[str, ...]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.crud import ConfigCRUD, UserCRUD

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    skipped = []
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            for key in ("disable_method", "disabled_group_id", "enhanced_details"):
                if (
                    key in data
                    and not (key == "disabled_group_id" and not data[key])
                    and not (key == "enhanced_details" and data[key] is None)
                ):
                    value = str(data[key]).lower() if key == "enhanced_details" else str(data[key])
                    await ConfigCRUD.set(session, key, value)
            for name, limit in data.get("limits", {}).get("special", {}).items():
                if limit is not None and limit < 1:
                    skipped.append(f"Skipped special limit for {name}: must be at least 1")
                    continue
                if await UserCRUD.get_by_username(session, name) is None:
                    await UserCRUD.create_or_update(session, name)
                await UserCRUD.set_special_limit(session, name, limit, fetch_from_panel=False)
            for name in data.get("except_users", []):
                if await UserCRUD.get_by_username(session, name) is None:
                    await UserCRUD.create_or_update(session, name)
                await UserCRUD.set_excepted(session, name, True, reason="Restored from backup", fetch_from_panel=False)
    finally:
        await engine.dispose()
    return tuple(skipped)


@dataclass
class RestoreResult:
    kind: str
    notes: tuple[str, ...] = ()


def _replace_offline(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as error:
        # A file bind mount cannot be renamed. With its writer stopped, copy the
        # prepared contents into that same mount; rollback uses this path too.
        if error.errno != errno.EBUSY or not os.path.ismount(target):
            raise
        shutil.copyfile(source, target)


def _container_database_roots(mountinfo: str, current: Path, owned_files: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    """Reject destinations that would disappear with the one-off container."""
    roots = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        point = Path(decoded)
        if point in (Path("/"), Path("/etc/opt/pg-limiter")) or any(
            point.is_relative_to(prefix) for prefix in ("/proc", "/sys", "/dev")
        ):
            continue
        # File mounts are safe for the configured database and owned state files.
        # Incidental mounted files must not become archive-selected destinations.
        if not point.is_dir() and point != current and point not in owned_files:
            continue
        roots.append(point)
    return tuple(roots)


def apply_backup(archive_path: Path | str, layout: BackupLayout, *, service_stopped: bool = False) -> RestoreResult:
    """Apply verified data only after the caller has proved service shutdown."""
    if service_stopped is not True:
        raise ValueError("Restore requires the service to be stopped; apply offline")
    with validate_backup(archive_path, layout) as plan:
        if plan.kind == "config":
            notes = asyncio.run(_import_legacy_config(plan.config_data, plan.database))
            return RestoreResult("config", notes)
        replacements, originals, applied, retained = {}, {}, [], set()
        try:
            # Prepare every replacement before the first target mutation.
            for target, source in plan.files.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                if source is not None:
                    fd, name = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
                    os.close(fd)
                    shutil.copyfile(source, name)
                    replacements[target] = Path(name)
            targets = list(plan.files) + [Path(str(plan.database) + suffix) for suffix in ("-wal", "-shm")]
            for target in targets:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise ValueError("Restore target is not a regular file")
                if target.exists():
                    fd, name = tempfile.mkstemp(prefix=".restore-old-", dir=target.parent)
                    os.close(fd)
                    shutil.copyfile(target, name)
                    originals[target] = Path(name)
            for target in targets:
                applied.append(target)
                if target in replacements:
                    _replace_offline(replacements[target], target)
                else:
                    target.unlink(missing_ok=True)
        except BaseException as error:
            failures = []
            for target in reversed(applied):
                try:
                    if target in originals:
                        _replace_offline(originals[target], target)
                    else:
                        target.unlink(missing_ok=True)
                except BaseException as rollback_error:
                    detail = f"{target}: {rollback_error}"
                    if target in originals:
                        retained.add(originals[target])
                        detail += f"; original recovery copy retained at {originals[target]}"
                    failures.append(detail)
            if failures:
                raise OSError(f"Restore failed ({error}); rollback incomplete: {' | '.join(failures)}") from error
            raise
        finally:
            for temporary in list(replacements.values()) + list(originals.values()):
                if temporary not in retained:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        # Cleanup failure must not hide the restore/rollback error
                        # or prevent cleanup of other private temporary files.
                        pass
        return RestoreResult(plan.kind, plan.notes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "validate", "apply"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    # A one-off Compose helper bypasses start.sh, which normally creates this
    # same persistent data symlink before the application imports its stores.
    if Path.cwd() == Path("/app") and not Path("data").exists():
        Path("data").symlink_to("/var/lib/pg-limiter/data", target_is_directory=True)
    try:
        layout = runtime_layout()
        if args.action != "create" and not layout.allowed_database_roots and Path("/.dockerenv").exists():
            roots = _container_database_roots(
                Path("/proc/self/mountinfo").read_text(),
                sqlite_path(layout.database_url),
                tuple(path.resolve() for path in layout.state_paths.values()),
            )
            if not roots:
                raise ValueError("No persistent database mount is available for offline restoration")
            layout.allowed_database_roots = roots
            layout.persistent_state_roots = roots
        if args.action == "create":
            create_backup(args.archive, layout)
            print("Database-consistent backup created")
        elif args.action == "validate":
            with validate_backup(args.archive, layout) as plan:
                print(f"Validated {plan.kind} restore")
        else:
            result = apply_backup(args.archive, layout, service_stopped=args.service_stopped)
            print(
                "SQLite database snapshot restored"
                if result.kind == "database"
                else "Legacy configuration imported transactionally; no database snapshot was replaced"
            )
            for note in result.notes:
                print(note)
        return 0
    except (ValueError, OSError) as error:
        print(f"Backup/restore failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
