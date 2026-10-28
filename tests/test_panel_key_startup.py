"""Execute startup and installer boundaries without live services or host writes."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = Path(__file__).resolve().parents[1]
KEY = "pg_key_SYNTHETIC_PRIVATE_TEST_VALUE"
LOG_FUNCTIONS = "\n".join(
    f'{name}() {{ printf "%s\\n" "$*"; }}' for name in ("log_step", "log_info", "log_warn", "log_error", "log_debug")
)


def _startup_fragment(start, end):
    source = (ROOT / "start.sh").read_text()
    return source[source.index(start) : source.index(end)]


def _run_env_gate(tmp_path, **settings):
    script = LOG_FUNCTIONS + "\n" + _startup_fragment("log_step 2 ", "# STEP 3:")
    script += '\nprintf "STARTUP_READY\\n"\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PANEL_DOMAIN": "panel.example.invalid",
            "BOT_TOKEN": "1:test",
            "ADMIN_IDS": "1",
            **settings,
        },
        text=True,
        capture_output=True,
        timeout=5,
    )


def test_startup_accepts_panel_key_without_panel_password(tmp_path):
    result = _run_env_gate(tmp_path, PANEL_API_KEY=KEY)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STARTUP_READY" in result.stdout
    assert KEY not in result.stdout + result.stderr


def test_legacy_panel_password_cannot_start_without_api_key(tmp_path):
    result = _run_env_gate(tmp_path, PANEL_USERNAME="legacy", PANEL_PASSWORD="legacy-password")
    assert result.returncode != 0
    assert "STARTUP_READY" not in result.stdout
    assert "PANEL_API_KEY" in result.stdout and ".env" in result.stdout


@pytest.mark.parametrize("status", [200, 401, 403])
def test_existing_panel_preflight_sends_only_api_key(tmp_path, status):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(KEY.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # A symlink outside the venv can lose pyvenv.cfg on Python 3.12/3.13.
    # Launch the configured interpreter directly so its dependencies stay in use.
    launcher = binaries / "python"
    launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    launcher.chmod(0o755)
    script = LOG_FUNCTIONS + "\n" + _startup_fragment("log_step 6 ", "# STEP 7:")
    script += '\nprintf "PREFLIGHT_FINISHED\\n"\n'
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "PANEL_DOMAIN": f"http://127.0.0.1:{server.server_port}",
                "PANEL_API_KEY": KEY,
                "PANEL_VERIFY_SSL": "true",
            },
            text=True,
            capture_output=True,
            timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(requests) == 1
    assert requests[0][0] == "/api/"
    assert requests[0][1].get("X-Api-Key") == KEY
    assert "Authorization" not in requests[0][1]
    assert KEY not in result.stdout + result.stderr
    assert "PREFLIGHT_FINISHED" in result.stdout


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (KEY, "'pg_key_SYNTHETIC_PRIVATE_TEST_VALUE'"),
        (r"""pg_key_$cash#hash&pipe|slash\quote'"tail""", r"""'pg_key_$cash#hash&pipe|slash\\quote\'"tail'"""),
    ],
)
def test_installer_stores_literal_panel_key_without_legacy_credentials(tmp_path, key, expected):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    env_file.chmod(0o600)
    script = r"""
source "$PG_LIMITER_TEST_SCRIPT" help >/dev/null
ENV_FILE="$PG_LIMITER_TEST_ENV_FILE"
create_env_file
configure_interactive
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PG_LIMITER_TEST_SCRIPT": str(ROOT / "pg-limiter.sh"),
            "PG_LIMITER_TEST_ENV_FILE": str(env_file),
        },
        input="\n".join(("panel.example.invalid", key, "1:bot", "1", "2", "180", "900", "", "UTC", "")),
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assignments = dict(
        line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#")
    )
    assert assignments["PANEL_API_KEY"] == expected
    assert "PANEL_PASSWORD" not in assignments and "PANEL_USERNAME" not in assignments
    assert key not in result.stdout + result.stderr
    assert env_file.stat().st_mode & 0o777 == 0o600
