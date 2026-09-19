"""Last-admin cardinality is enforced in the storage mutation itself, so no
path (command continuation or callback) can remove the final administrator."""

import copy

import pytest

import telegram_bot.utils as tgutils


async def test_last_admin_cannot_be_removed(monkeypatch):
    state = {"telegram": {"admins": [111]}}
    written = []

    async def fake_read():
        return copy.deepcopy(state)

    async def fake_write(data):
        written.append(data)

    monkeypatch.setenv("ADMIN_IDS", "")
    monkeypatch.setattr(tgutils, "read_json_file", fake_read)
    monkeypatch.setattr(tgutils, "write_json_file", fake_write)

    with pytest.raises(tgutils.LastAdminError):
        await tgutils.remove_admin_from_config(111)

    # The sole admin must remain: nothing was written.
    assert written == []
