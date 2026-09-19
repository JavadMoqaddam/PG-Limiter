"""Top-level entry points must not parse argv at import or read the retired
disabled-users JSON."""

import sys

import pytest


def test_limiter_parse_args_is_not_run_at_import(monkeypatch):
    # Import under argv that would fail argparse; a clean import proves parsing
    # no longer happens at module load.
    monkeypatch.setattr(sys, "argv", ["pytest", "--unknown-flag", "value"])
    import limiter

    assert hasattr(limiter, "parse_args")
    # Explicit parsing still works when asked.
    ns = limiter.parse_args([])
    assert ns is not None
    with pytest.raises(SystemExit):
        limiter.parse_args(["--version"])


def test_cli_status_disabled_count_uses_registry(monkeypatch):
    import cli_main

    async def fake_disabled_usernames():
        return {"alice", "bob", "carol"}

    monkeypatch.setattr(
        "utils.handel_dis_users.disabled_usernames", fake_disabled_usernames
    )

    assert cli_main._disabled_user_count() == 3
