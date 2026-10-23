"""Execute the real management restore flow with external Docker replaced."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from test_backup_restore import layout  # noqa: F401


@pytest.fixture
def shell_restore(tmp_path, layout):
    from utils.backup import create_backup

    archive = tmp_path / "archive.zip"
    create_backup(archive, layout)
    database = Path(layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE committed SET value='newer'")
    binary = tmp_path / "bin"
    binary.mkdir()
    operations = tmp_path / "docker.jsonl"
    state = tmp_path / "container-state"
    state.write_text("true")
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with open(os.environ["TEST_DOCKER_LOG"], "a") as handle:
    handle.write(json.dumps(args) + "\n")
state = pathlib.Path(os.environ["TEST_DOCKER_STATE"])
if args[:2] == ["compose", "version"]:
    sys.exit(0)
if args and args[0] == "ps":
    if os.environ.get("TEST_INSPECT_FAIL"):
        sys.exit(3)
    print("" if os.environ.get("TEST_CUSTOM_NAME") else "container-id")
    sys.exit(0)
if args and args[0] == "inspect":
    if os.environ.get("TEST_INSPECT_FAIL"):
        sys.exit(3)
    print("true" if args[-1] == "extra-running-id" else state.read_text())
    sys.exit(0)
if args and args[0] == "compose":
    if "stop" in args or "down" in args:
        if os.environ.get("TEST_STOP_FAIL"):
            sys.exit(2)
        if not os.environ.get("TEST_STOP_KEEPS_RUNNING"):
            state.write_text("false")
        sys.exit(0)
    if "ps" in args:
        if os.environ.get("TEST_POST_DISCOVERY_FAIL") and state.read_text() == "false":
            sys.exit(4)
        if "-q" in args:
            print("custom-container-id" if os.environ.get("TEST_CUSTOM_NAME") else "container-id")
            if os.environ.get("TEST_EXTRA_RUNNING_ID"):
                print("extra-running-id")
        else:
            print("pg-limiter running" if state.read_text() == "true" else "")
        sys.exit(0)
    if "run" in args:
        offset = args.index("-m")
        assert args[offset + 1] == "utils.backup"
        code = """
import os, sys
from pathlib import Path
import utils.backup as backup
layout = backup.BackupLayout(os.environ['DATABASE_URL'], Path(os.environ['TEST_ENV_PATH']),
    {role: Path(path) for role, path in __import__('json').loads(os.environ['TEST_STATE_PATHS']).items()},
    allowed_database_roots=(Path(os.environ['TEST_ROOT']),))
backup.runtime_layout = lambda: layout
sys.argv = ['backup'] + sys.argv[1:]
raise SystemExit(backup.main())
"""
        sys.exit(subprocess.call([sys.executable, "-c", code, *args[offset + 2:]]))
    if "up" in args:
        state.write_text("true")
        sys.exit(0)
sys.exit(0)
'''
    )
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"])
    env.update(
        TEST_DOCKER_LOG=str(operations),
        TEST_DOCKER_STATE=str(state),
        DATABASE_URL=layout.database_url,
        TEST_ENV_PATH=str(layout.env_path),
        TEST_STATE_PATHS=json.dumps({k: str(v) for k, v in layout.state_paths.items()}),
        TEST_ROOT=str(tmp_path),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    script = Path(__file__).resolve().parents[1] / "pg-limiter.sh"

    def run(path=archive, **settings):
        command = r"""
source "$TEST_MANAGER" help >/dev/null
CONFIG_DIR="$TEST_CONFIG_DIR"
DATA_DIR="$TEST_DATA_DIR"
COMPOSE_FILE="$CONFIG_DIR/docker-compose.yml"
ENV_FILE="$CONFIG_DIR/.env"
touch "$COMPOSE_FILE"
check_running_as_root() { :; }
detect_os() { :; }
detect_and_update_package_manager() { :; }
ensure_zip_installed() { :; }
check_docker() { detect_compose; }
cmd_restore "$TEST_ARCHIVE"
"""
        task_env = dict(
            env,
            TEST_MANAGER=str(script),
            TEST_CONFIG_DIR=str(layout.env_path.parent),
            TEST_DATA_DIR=str(tmp_path / "persistent"),
            TEST_ARCHIVE=str(path),
            **settings,
        )
        result = subprocess.run(["bash", "-c", command], input="y\nn\n", text=True, capture_output=True, env=task_env)
        calls = [json.loads(line) for line in operations.read_text().splitlines()] if operations.exists() else []
        return result, calls

    return run, database, state, archive


def test_actual_host_command_validates_then_stops_then_applies_offline(shell_restore):
    run, database, state, _ = shell_restore
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]
    phases = [next((phase for phase in ("validate", "stop", "apply", "up") if phase in call), None) for call in calls]
    phases = [phase for phase in phases if phase]
    assert phases == ["validate", "stop", "apply"]
    assert "--service-stopped" in next(call for call in calls if "apply" in call)
    assert state.read_text() == "false"


@pytest.mark.parametrize("failure", ["TEST_STOP_FAIL", "TEST_INSPECT_FAIL"])
def test_stop_or_status_failure_prevents_host_apply(shell_restore, failure):
    run, database, _, _ = shell_restore
    result, calls = run(**{failure: "1"})
    assert result.returncode != 0
    assert not any("apply" in call for call in calls)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("newer",)]


def test_invalid_archive_is_rejected_before_service_stop(shell_restore, tmp_path):
    run, database, state, _ = shell_restore
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a backup")
    result, calls = run(invalid)
    assert result.returncode != 0
    assert not any("stop" in call or "down" in call or "apply" in call for call in calls)
    assert state.read_text() == "true"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("newer",)]


def test_custom_compose_container_is_discovered_and_inspected_before_apply(shell_restore):
    run, database, _, _ = shell_restore
    result, calls = run(TEST_CUSTOM_NAME="1")
    assert result.returncode == 0, result.stdout + result.stderr
    discovery = [call for call in calls if "compose" in call and "ps" in call and "-q" in call]
    assert len(discovery) == 2
    assert all("-a" in call for call in discovery)
    assert any(call[0] == "inspect" and call[-1] == "custom-container-id" for call in calls)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("original",)]


@pytest.mark.parametrize("failure", ["TEST_STOP_KEEPS_RUNNING", "TEST_POST_DISCOVERY_FAIL", "TEST_EXTRA_RUNNING_ID"])
def test_custom_service_running_or_failed_discovery_prevents_apply(shell_restore, failure):
    run, database, _, _ = shell_restore
    result, calls = run(TEST_CUSTOM_NAME="1", **{failure: "1"})
    assert result.returncode != 0
    assert not any("apply" in call for call in calls)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [("newer",)]
