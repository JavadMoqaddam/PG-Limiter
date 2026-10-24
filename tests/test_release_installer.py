"""Run actual installer validation with downloads and host installation replaced."""

import os
import re
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "pg-limiter.sh"


@pytest.mark.parametrize(
    "download",
    [
        "valid",
        "valid_previous_version",
        "wrong_version",
        "duplicate_wrong_version",
        "matching_line_after_dispatcher",
        "duplicate_matching_version",
        "matching_line_in_function",
        "inline_wrong_version",
        "export_wrong_version",
        "broken_syntax",
        "truncated",
        "empty",
        "html",
        "curl_failure",
    ],
)
def test_install_fetches_matching_tag_and_refuses_invalid_download(tmp_path, download):
    source = SCRIPT.read_text()
    if download == "valid_previous_version":
        source = source.replace('VERSION="1.7.0"', 'VERSION="1.6.1"', 1)
    version = re.search(r'^VERSION="([^"]+)"$', source, re.MULTILINE).group(1)
    assignment = f'VERSION="{version}"'
    candidate = source
    if download == "wrong_version":
        candidate = candidate.replace(assignment, 'VERSION="0.0.1"', 1)
    elif download == "duplicate_wrong_version":
        candidate = candidate.replace(assignment, assignment + '\nVERSION="0.0.1"', 1)
    elif download == "matching_line_after_dispatcher":
        candidate = candidate.replace(assignment, 'VERSION="0.0.1"', 1) + f"\n{assignment}\n"
    elif download == "duplicate_matching_version":
        candidate = candidate.replace(assignment, assignment + f"\n{assignment}", 1)
    elif download == "matching_line_in_function":
        candidate = candidate.replace(assignment, 'VERSION="0.0.1"', 1)
        candidate = candidate.replace("cmd_help() {", f"cmd_help() {{\n{assignment}", 1)
    elif download == "inline_wrong_version":
        candidate = candidate.replace(assignment, assignment + '\n:; VERSION="0.0.1"', 1)
    elif download == "export_wrong_version":
        candidate = candidate.replace(assignment, assignment + '\nexport VERSION="0.0.1"', 1)
    elif download == "broken_syntax":
        candidate = f'#!/usr/bin/env bash\nVERSION="{version}"\ncmd_update() {{\n'
    elif download == "truncated":
        candidate = f'#!/usr/bin/env bash\nVERSION="{version}"\ncmd_update() {{ :; }}\n'
    elif download == "empty":
        candidate = ""
    elif download == "html":
        candidate = "<html>not found</html>\n"
    canary = tmp_path / "candidate-executed"
    candidate += f'\ntouch "{canary}"\n'
    downloaded = tmp_path / "downloaded.sh"
    downloaded.write_text(candidate)
    caller = tmp_path / "caller.sh"
    caller.write_text(source)
    target = tmp_path / "installed"
    previous = b"previous installed CLI\n"
    target.write_bytes(previous)
    target.chmod(0o700)
    calls = tmp_path / "curl-calls"
    command = r"""
source "$TEST_SOURCE" help >/dev/null
colorized_echo() { :; }
mktemp() { command mktemp "$TEST_ROOT/download.XXXXXX"; }
curl() {
    printf '%s\n' "$*" > "$TEST_CALLS"
    cp "$TEST_DOWNLOAD" "${@: -1}"
    if [[ "$TEST_KIND" == curl_failure ]]; then return 22; fi
}
install() { command install -m "$2" "$3" "$TEST_TARGET"; }
install_script
"""
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "TEST_SOURCE": str(caller),
            "TEST_DOWNLOAD": str(downloaded),
            "TEST_TARGET": str(target),
            "TEST_CALLS": str(calls),
            "TEST_ROOT": str(tmp_path),
            "TEST_KIND": download,
        },
        text=True,
        capture_output=True,
    )
    assert f"/v{version}/pg-limiter.sh" in calls.read_text()
    assert not canary.exists()
    assert not list(tmp_path.glob("download.*"))
    if download in {"valid", "valid_previous_version"}:
        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == downloaded.read_bytes()
        assert target.stat().st_mode & 0o777 == 0o755
    else:
        assert result.returncode != 0
        assert target.read_bytes() == previous
        assert target.stat().st_mode & 0o777 == 0o700


def test_failed_image_pull_keeps_previous_compose_and_restarts_it(tmp_path):
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    compose = config / "docker-compose.yml"
    compose.write_text("previous compose\n")
    env_path = config / ".env"
    env_path.write_text("previous config\n")
    calls = tmp_path / "calls"
    command = r"""
source "$TEST_SOURCE" help >/dev/null
colorized_echo() { :; }
CONFIG_DIR="$TEST_CONFIG"
DATA_DIR="$TEST_DATA"
COMPOSE_FILE="$CONFIG_DIR/docker-compose.yml"
ENV_FILE="$CONFIG_DIR/.env"
COMPOSE=compose
docker() {
    printf 'docker %s\n' "$*" >> "$TEST_CALLS"
    if [[ "$1" == pull ]]; then return 1; fi
    return 0
}
compose() { printf 'compose %s\n' "$*" >> "$TEST_CALLS"; }
create_compose_file() { printf 'unexpected regeneration\n' >> "$TEST_CALLS"; }
cmd_update_stage2
"""
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "TEST_SOURCE": str(SCRIPT),
            "TEST_CONFIG": str(config),
            "TEST_DATA": str(data),
            "TEST_CALLS": str(calls),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert compose.read_text() == "previous compose\n" and env_path.read_text() == "previous config\n"
    assert "up -d" in calls.read_text() and "unexpected regeneration" not in calls.read_text()
    assert "pull ghcr.io/javadmoqaddam/pg-limiter:" in calls.read_text()
    assert "pull ghcr.io/javadmoqaddam/pg-limiter:latest" not in calls.read_text()
