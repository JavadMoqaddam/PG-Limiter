"""Optional Telegram connectivity must not prevent daemon startup."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario", ["connected", "network", "timeout", "outer_timeout", "throttled"])
def test_startup_continues_when_telegram_is_unavailable(tmp_path, scenario):
    result = _run_telegram_preflight(tmp_path, scenario)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DAEMON_START_REACHED" in result.stdout


@pytest.mark.parametrize("scenario", ["invalid_token", "bad_request", "unexpected"])
def test_startup_still_rejects_invalid_credentials_and_local_errors(tmp_path, scenario):
    result = _run_telegram_preflight(tmp_path, scenario)
    assert result.returncode != 0
    assert "DAEMON_START_REACHED" not in result.stdout


def _run_telegram_preflight(tmp_path, scenario):
    """Execute the actual startup gate with a mocked external Telegram API."""
    package = tmp_path / "telegram"
    package.mkdir()
    (package / "error.py").write_text(
        "class NetworkError(Exception): pass\n"
        "class TimedOut(NetworkError): pass\n"
        "class InvalidToken(Exception): pass\n"
        "class BadRequest(NetworkError): pass\n"
        "class RetryAfter(Exception): pass\n"
    )
    (package / "__init__.py").write_text(
        "import os\n"
        "from types import SimpleNamespace\n"
        "from .error import BadRequest, InvalidToken, NetworkError, RetryAfter, TimedOut\n"
        "class Bot:\n"
        "    def __init__(self, token):\n"
        "        if os.environ['PG_LIMITER_TEST_TELEGRAM'] == 'invalid_token':\n"
        "            raise InvalidToken('invalid test token')\n"
        "    async def get_me(self):\n"
        "        scenario = os.environ['PG_LIMITER_TEST_TELEGRAM']\n"
        "        if scenario == 'network': raise NetworkError('network unavailable')\n"
        "        if scenario == 'timeout': raise TimedOut('API timeout')\n"
        "        if scenario == 'throttled': raise RetryAfter('API rate limit')\n"
        "        if scenario == 'bad_request': raise BadRequest('invalid API request')\n"
        "        if scenario == 'unexpected': raise RuntimeError('local failure')\n"
        "        return SimpleNamespace(username='test_bot', id=123)\n"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    timeout = binaries / "timeout"
    timeout.write_text(
        '#!/bin/sh\nif [ "$PG_LIMITER_TEST_TELEGRAM" = "outer_timeout" ]; then exit 124; fi\nshift\nexec "$@"\n'
    )
    timeout.chmod(0o755)
    source = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
    gate = source[source.index("log_step 5 ") : source.index("# STEP 6:")]
    script = (
        'log_step() { :; }\nlog_info() { printf "%s\\n" "$*"; }\n'
        'log_warn() { printf "%s\\n" "$*"; }\nlog_error() { printf "%s\\n" "$*"; }\n'
        + gate
        + '\nprintf "DAEMON_START_REACHED\\n"\n'
    )
    env = {
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(tmp_path),
        "BOT_TOKEN": "123:test_token",
        "PG_LIMITER_TEST_TELEGRAM": scenario,
    }
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
