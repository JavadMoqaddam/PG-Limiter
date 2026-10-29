"""Exercise the real shell supervisor against harmless child programs."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


def supervisor(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
    # Skip installation/startup I/O, retaining the actual traps, Python wrapper
    # and restart loop. Only the supervised application is replaced.
    prefix = source.split('echo "╔', 1)[0]
    body = "run_limiter() {" + source.split("run_limiter() {", 1)[1]
    script = tmp_path / "supervisor.sh"
    script.write_text(prefix + "\nRESTART_DELAY=0\n" + body)
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    return subprocess.Popen(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_parent_signal_reaches_child_and_waits_for_cleanup(tmp_path, signum):
    (tmp_path / "limiter.py").write_text(
        "from pathlib import Path\nimport time\n"
        "Path('ready').write_text('ready')\n"
        "try:\n    while True: time.sleep(1)\n"
        "finally:\n    time.sleep(0.1)\n    Path('cleaned').write_text('cleaned')\n"
    )
    process = supervisor(tmp_path)
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "ready").exists()
        process.send_signal(signum)
        assert process.wait(timeout=1) == 0
        assert (tmp_path / "cleaned").exists()
    finally:
        stop_group(process)


def test_child_crash_restarts_fresh_interpreter(tmp_path):
    (tmp_path / "limiter.py").write_text(
        "from pathlib import Path\nimport os, sys\n"
        "path = Path('launches')\n"
        "previous = path.read_text() if path.exists() else ''\n"
        "path.write_text(previous + str(os.getpid()) + '\\n')\n"
        "sys.exit(2 if not previous else 0)\n"
    )
    process = supervisor(tmp_path)
    try:
        assert process.wait(timeout=3) == 0
        launches = (tmp_path / "launches").read_text().splitlines()
        assert len(launches) == 2 and len(set(launches)) == 2
    finally:
        stop_group(process)
