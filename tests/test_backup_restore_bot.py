"""Telegram restore only stages; canonical legacy imports happen offline."""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from test_backup_restore import layout  # noqa: F401


def _upload(name, payload):
    update = MagicMock()
    update.message.reply_html = AsyncMock()
    update.message.document.file_name = name
    file = MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(payload)))
    update.message.document.get_file = AsyncMock(return_value=file)
    return update


@pytest.fixture
def initialized_layout(layout):
    from sqlalchemy import create_engine
    from db.models import Base

    engine = create_engine(layout.database_url.replace("sqlite+aiosqlite", "sqlite"))
    Base.metadata.create_all(engine)
    engine.dispose()
    return layout


@pytest.mark.parametrize("kind", ["zip", "json"])
async def test_bot_stages_upload_without_mutating_live_state(initialized_layout, tmp_path, monkeypatch, kind):
    import telegram_bot.handlers.backup as handler
    from utils.backup import create_backup
    from utils.read_config import get_config_revision

    layout = initialized_layout
    stage_dir = tmp_path / "restore-staging"
    monkeypatch.setattr(handler, "RESTORE_STAGING_DIR", str(stage_dir))
    monkeypatch.setattr(handler, "runtime_layout", lambda **kwargs: layout)
    monkeypatch.setattr(handler, "check_admin_privilege", AsyncMock(return_value=None))
    database = Path(layout.database_url.split("///", 1)[1])
    before = database.read_bytes(), layout.env_path.read_bytes()
    if kind == "zip":
        archive = tmp_path / "backup.zip"
        create_backup(archive, layout)
        payload = archive.read_bytes()
    else:
        payload = json.dumps(
            {"disable_method": "group", "limits": {"special": {"alice": 3}}, "except_users": ["bob"]}
        ).encode()
    update = _upload(f"backup.{kind}", payload)
    context = MagicMock(user_data={"waiting_for": "restore"})
    revision = get_config_revision()
    await handler.restore_config_handler(update, context)
    staged = list(stage_dir.iterdir())
    assert len(staged) == 1
    assert staged[0].read_bytes() == payload
    assert (database.read_bytes(), layout.env_path.read_bytes()) == before
    assert get_config_revision() == revision
    replies = " ".join(call.args[0] for call in update.message.reply_html.call_args_list)
    assert f"pg-limiter restore {staged[0]}" in replies
    assert "staged" in replies and "restored successfully" not in replies
    assert context.user_data["waiting_for"] is None


async def test_offline_legacy_json_import_uses_one_canonical_transaction(initialized_layout, tmp_path):
    from db.crud.config_revision import REVISION_KEY
    from utils.backup import apply_backup, stage_backup

    layout = initialized_layout
    database = Path(layout.database_url.split("///", 1)[1])
    payload = json.dumps(
        {
            "disable_method": "group",
            "disabled_group_id": 9,
            "enhanced_details": False,
            "limits": {"special": {"alice": 3, "invalid": 0}},
            "except_users": ["bob"],
        }
    ).encode()
    staged = stage_backup(payload, layout, tmp_path / "staging")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM config").fetchone() == (0,)
    result = await asyncio.to_thread(apply_backup, staged, layout, service_stopped=True)
    assert result.kind == "config"
    assert "invalid" in " ".join(result.notes)
    with sqlite3.connect(database) as connection:
        settings = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM config")}
        version = settings.pop(REVISION_KEY)
        assert set(version) == {"revision"}
        assert type(version["revision"]) is int and version["revision"] > 0
        assert settings == {
            "disable_method": "group",
            "disabled_group_id": "9",
            "enhanced_details": "false",
        }
        assert connection.execute("SELECT special_limit FROM users WHERE username='alice'").fetchone() == (3,)
        assert connection.execute("SELECT is_excepted FROM users WHERE username='bob'").fetchone() == (1,)


async def test_failed_legacy_config_transaction_does_not_commit_earlier_settings(
    initialized_layout, tmp_path, monkeypatch
):
    from db.crud import UserCRUD
    from utils.backup import apply_backup

    async def fail(*args, **kwargs):
        raise RuntimeError("persistence failure")

    monkeypatch.setattr(UserCRUD, "create_or_update", fail)
    path = tmp_path / "legacy.json"
    path.write_text('{"disable_method":"group","limits":{"special":{"alice":3}}}')
    with pytest.raises(RuntimeError, match="persistence failure"):
        await asyncio.to_thread(apply_backup, path, initialized_layout, service_stopped=True)
    database = Path(initialized_layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM config").fetchone() == (0,)


async def test_restore_rechecks_admin_before_downloading(monkeypatch):
    import telegram_bot.handlers.backup as handler

    monkeypatch.setattr(handler, "check_admin_privilege", AsyncMock(return_value=-1))
    update = _upload("backup.zip", b"irrelevant")
    assert await handler.restore_config_handler(update, MagicMock()) == -1
    update.message.document.get_file.assert_not_awaited()


async def test_immediate_auto_backup_reports_missing_database_as_failure(layout, monkeypatch, tmp_path):
    import telegram_bot.handlers.backup as handler
    import telegram_bot.main as main

    monkeypatch.chdir(tmp_path)
    Path(layout.database_url.split("///", 1)[1]).unlink()
    monkeypatch.setattr(handler, "runtime_layout", lambda **kwargs: layout)
    monkeypatch.setattr(handler, "check_admin_privilege", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "application", MagicMock())
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    await handler.auto_backup_now(update, MagicMock())
    reply = update.callback_query.edit_message_text.call_args.kwargs["text"]
    assert "successfully" not in reply
    assert "failed" in reply.lower()


async def test_offline_config_import_preserves_existing_user_metadata(initialized_layout, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db.models import User
    from utils.backup import apply_backup

    engine = create_engine(initialized_layout.database_url.replace("sqlite+aiosqlite", "sqlite"))
    with Session(engine) as session, session.begin():
        session.add(User(username="alice", status="disabled", owner_id=12, group_ids=[7], used_traffic=40))
    engine.dispose()
    path = tmp_path / "legacy.json"
    path.write_text('{"limits":{"special":{"alice":4}},"except_users":["alice"]}')
    await asyncio.to_thread(apply_backup, path, initialized_layout, service_stopped=True)
    database = Path(initialized_layout.database_url.split("///", 1)[1])
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status, owner_id, group_ids, used_traffic FROM users WHERE username='alice'"
        ).fetchone() == (
            "disabled",
            12,
            "[7]",
            40,
        )
