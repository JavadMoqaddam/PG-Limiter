#!/usr/bin/env python3
"""
Import every module in the project and fail on the ones that are actually broken.

`python -m compileall .` - the only whole-repo check CI had - proves each file
parses. It cannot see a name that no longer exists: after a rename, every stale
call site still compiles perfectly. A missing import, a helper moved to another
module, a symbol dropped from an `__init__` re-export - all of it stays green
until the code happens to run, which for a background loop can be hours after
deploy and for a Telegram handler can be the first time someone presses a button.

Third-party packages that are simply not installed are reported and skipped, so
this stays runnable on a laptop without the full requirements.txt.
"""

import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Import failures naming one of these are our own code and always fail the test.
OUR_TOP_LEVEL = {
    "api",
    "api_server",
    "cli",
    "cli_main",
    "db",
    "limiter",
    "run_telegram",
    "telegram_bot",
    "tools",
    "utils",
}

PACKAGE_DIRS = ("api", "cli", "db", "telegram_bot", "utils")
ROOT_MODULES = ("api_server", "cli_main", "limiter", "run_telegram")

# Alembic revisions are loaded by Alembic with a migration context, and their file
# names start with a digit, so they are not importable as modules at all.
SKIP_PARTS = {"__pycache__", "migrations"}


def _iter_module_names():
    for package in PACKAGE_DIRS:
        base = PROJECT_ROOT / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(PROJECT_ROOT).with_suffix("").parts
            if any(part in SKIP_PARTS for part in parts):
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                yield ".".join(parts)
    for module in ROOT_MODULES:
        if (PROJECT_ROOT / f"{module}.py").is_file():
            yield module


def test_every_module_imports():
    # limiter.py calls parser.parse_args() at import time; under pytest sys.argv
    # carries pytest's own flags, which argparse would reject with SystemExit(2).
    original_argv = sys.argv
    sys.argv = [original_argv[0]]

    imported: list[str] = []
    fatal: list[str] = []
    skipped: list[str] = []

    try:
        for name in _iter_module_names():
            try:
                importlib.import_module(name)
                imported.append(name)
            except ModuleNotFoundError as error:
                missing = (error.name or "").split(".")[0]
                if missing in OUR_TOP_LEVEL:
                    fatal.append(f"{name}: {error}")
                else:
                    skipped.append(f"{name}: needs '{missing}', which is not installed")
            except (ImportError, SyntaxError, NameError, AttributeError) as error:
                # "cannot import name X from Y", a typo in a module-level constant,
                # an attribute that moved - defects in this repository, every one.
                fatal.append(f"{name}: {type(error).__name__}: {error}")
            except BaseException as error:  # pylint: disable=broad-except
                # Anything else is the environment talking: no config file, no
                # writable log directory, no network. Report, do not fail.
                skipped.append(f"{name}: {type(error).__name__}: {error}")
    finally:
        sys.argv = original_argv

    for note in skipped:
        print(f"skipped {note}")
    print(f"imported {len(imported)} modules, skipped {len(skipped)}")

    assert imported, "the import sweep found no modules at all - the directory walk is broken"
    assert not fatal, "modules failed to import:\n  " + "\n  ".join(fatal)
