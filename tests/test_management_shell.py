"""Exercise container CLI and shell commands without inherited Compose initialization."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "pg-limiter.sh"


@pytest.fixture
def management_shell(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    compose = config / "docker-compose.yml"
    compose.write_text("services: {}\n")
    (config / ".env").write_text("")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls_path = tmp_path / "docker-calls.jsonl"
    docker_source = (
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_DOCKER_CALLS"], "a") as handle:
    handle.write(json.dumps([name, *args]) + "\n")
if name == "docker":
    if args == ["compose", "version"]:
        sys.exit(0 if os.environ["TEST_COMPOSE_KIND"] == "plugin" else 1)
    if not args or args.pop(0) != "compose":
        sys.exit(9)
elif args == ["version"]:
    sys.exit(0)
assert args[:2] == ["-f", os.environ["TEST_COMPOSE_FILE"]], args
command = args[2:]
if command == ["ps", "--status", "running"]:
    print("pg-limiter running" if os.environ["TEST_RUNNING"] == "yes" else "")
elif command[:2] == ["exec", "pg-limiter"]:
    print("container command reached")
else:
    sys.exit(9)
"""
    )
    for name in ("docker", "docker-compose"):
        binary = binaries / name
        binary.write_text(docker_source)
        binary.chmod(0o755)
    fake_id = binaries / "id"
    fake_id.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_UID"\n')
    fake_id.chmod(0o755)

    def run(action, *, compose_kind="plugin", running=True, uid="0"):
        env = dict(os.environ)
        env.pop("COMPOSE", None)
        env.update(
            PATH=str(binaries) + os.pathsep + env["PATH"],
            TEST_SCRIPT=str(SCRIPT),
            TEST_COMPOSE_FILE=str(compose),
            TEST_ENV_FILE=str(config / ".env"),
            TEST_DOCKER_CALLS=str(calls_path),
            TEST_COMPOSE_KIND=compose_kind,
            TEST_RUNNING="yes" if running else "no",
            TEST_UID=uid,
        )
        command = r"""
source "$TEST_SCRIPT" help >/dev/null
COMPOSE_FILE="$TEST_COMPOSE_FILE"
ENV_FILE="$TEST_ENV_FILE"
if [[ "$1" == cli ]]; then cmd_cli "$@"; else cmd_shell; fi
"""
        result = subprocess.run(
            ["bash", "-c", command, "management-test", action, "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
        )
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
        return result, calls, str(compose)

    return run


@pytest.mark.parametrize("action", ["cli", "shell"])
@pytest.mark.parametrize("compose_kind", ["plugin", "standalone"])
@pytest.mark.parametrize("running", [True, False])
def test_container_commands_check_actual_service_state(management_shell, action, compose_kind, running):
    result, calls, compose = management_shell(action, compose_kind=compose_kind, running=running)
    prefix = ["docker", "compose"] if compose_kind == "plugin" else ["docker-compose"]
    assert prefix + ["-f", compose, "ps", "--status", "running"] in calls
    exec_calls = [call for call in calls if "exec" in call]
    if running:
        assert result.returncode == 0, result.stdout + result.stderr
        expected = ["python", "cli_main.py", "status"] if action == "cli" else ["/bin/bash"]
        assert exec_calls == [prefix + ["-f", compose, "exec", "pg-limiter", *expected]]
        assert "container command reached" in result.stdout
    else:
        assert result.returncode != 0
        assert "not running" in result.stdout
        assert exec_calls == []


@pytest.mark.parametrize("action", ["cli", "shell"])
def test_container_commands_still_require_root(management_shell, action):
    result, calls, _compose = management_shell(action, uid="1000")
    assert result.returncode != 0
    assert "must be run as root" in result.stdout
    assert calls == []
