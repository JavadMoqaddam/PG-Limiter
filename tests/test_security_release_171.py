"""Management boundaries must fail closed without revealing credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor
import asyncio
import logging
import shutil
import ssl
import subprocess

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from starlette.requests import Request

import api_server
import telegram_bot.handlers.settings_groups as groups
from utils.read_config import load_env_config


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setenv("API_USERNAME", "audit-admin")
    monkeypatch.setenv("API_PASSWORD", "synthetic-api-secret")
    monkeypatch.setattr(api_server, "_failed_auth_attempts", {})
    return Request({"type": "http", "client": ("192.0.2.1", 1234), "headers": []})


def authenticate(request, password):
    return api_server.verify_credentials(request, HTTPBasicCredentials(username="audit-admin", password=password))


def exhaust_auth_budget(request):
    for _ in range(5):
        with pytest.raises(HTTPException) as error:
            authenticate(request, "incorrect")
        assert error.value.status_code == 401


def test_real_bootstrap_config_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("API_HOST", raising=False)
    assert api_server._resolve_bind_host() == "127.0.0.1"


def test_correct_guess_is_refused_during_failed_auth_cooldown(auth):
    exhaust_auth_budget(auth)
    with pytest.raises(HTTPException) as error:
        authenticate(auth, "synthetic-api-secret")
    assert error.value.status_code == 429
    assert 1 <= int(error.value.headers["Retry-After"]) <= 60


def test_auth_cooldown_does_not_lock_another_client(auth):
    exhaust_auth_budget(auth)
    other = Request({"type": "http", "client": ("192.0.2.2", 1234), "headers": []})
    assert authenticate(other, "synthetic-api-secret") == "audit-admin"


def test_auth_recovers_after_the_window_without_extending_it(auth, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(api_server, "_auth_now", lambda: clock.now, raising=False)
    exhaust_auth_budget(auth)
    clock.now = 159
    with pytest.raises(HTTPException) as error:
        authenticate(auth, "synthetic-api-secret")
    assert error.value.status_code == 429 and error.value.headers["Retry-After"] == "1"
    clock.now = 160
    assert authenticate(auth, "synthetic-api-secret") == "audit-admin"


def test_parallel_failed_auth_attempts_share_the_same_budget(auth):
    def attempt(_):
        try:
            authenticate(auth, "incorrect")
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(attempt, range(20)))
    assert statuses.count(401) == 5
    assert statuses.count(429) == 15


def test_auth_failure_storage_is_bounded_and_expires(auth, monkeypatch):
    monkeypatch.setattr(api_server, "_AUTH_MAX_CLIENTS", 2)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(api_server, "_auth_now", lambda: clock.now)
    for host in ("192.0.2.10", "192.0.2.11", "192.0.2.12"):
        peer = Request({"type": "http", "client": (host, 1234), "headers": []})
        with pytest.raises(HTTPException):
            authenticate(peer, "incorrect")
    assert len(api_server._failed_auth_attempts) == 2
    clock.now = 160
    with pytest.raises(HTTPException):
        authenticate(auth, "incorrect")
    assert list(api_server._failed_auth_attempts) == ["192.0.2.1"]


async def test_config_response_masks_database_credentials_without_mutating_settings(auth, monkeypatch):
    config = load_env_config()
    config["database_url"] = "postgresql+asyncpg://operator:synthetic-db-secret@db.invalid/limiter?token=private"
    config["panel"]["api_key"] = "synthetic-panel-secret"
    monkeypatch.setattr(api_server, "_read_settings", AsyncMock(return_value=config))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url="http://test") as client:
        response = await client.get("/config", auth=("audit-admin", "synthetic-api-secret"))
    assert response.status_code == 200
    assert "synthetic-db-secret" not in response.text
    assert "token=private" not in response.text
    assert "synthetic-panel-secret" not in response.text
    assert config["panel"]["api_key"] == "synthetic-panel-secret"
    assert "synthetic-db-secret" in config["database_url"]


@pytest.mark.parametrize("handler", ["handle_select_disabled_group_callback", "handle_select_fallback_group_callback"])
async def test_panel_group_name_cannot_inject_telegram_html(monkeypatch, handler):
    hostile_name = '<a href="https://example.invalid">Click</a> & name'
    monkeypatch.setattr(groups, "save_config_value", AsyncMock())
    monkeypatch.setattr(groups, "read_config", AsyncMock(return_value={}))
    monkeypatch.setattr(
        groups, "_get_groups_from_panel", AsyncMock(return_value=([{"id": 7, "name": hostile_name}], {}))
    )
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await getattr(groups, handler)(query, SimpleNamespace(user_data={}), 7)
    text = query.edit_message_text.call_args.kwargs["text"]
    assert '<a href="https://example.invalid">' not in text
    assert "&lt;a href=" in text and "&amp; name" in text


async def test_panel_tls_rejects_untrusted_peer_and_accepts_explicit_ca(tmp_path, monkeypatch):
    """Use a real local TLS peer; an untrusted server must not receive the key."""
    from utils.panel_api import request_helper

    if not shutil.which("openssl"):
        pytest.skip("openssl is required for the loopback certificate test")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    requests = []

    async def serve(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=tls)
    url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/"
    monkeypatch.delenv("PANEL_VERIFY_SSL", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(request_helper, "_panel_client", None)
    async with server:
        try:
            client = await request_helper.get_panel_client()
            with pytest.raises(httpx.ConnectError):
                await client.get(url, headers={"X-Api-Key": "synthetic-panel-secret"})
            assert requests == []
            await request_helper.close_panel_client()
            monkeypatch.setenv("SSL_CERT_FILE", str(cert))
            client = await request_helper.get_panel_client()
            assert (await client.get(url, headers={"X-Api-Key": "synthetic-panel-secret"})).status_code == 200
            assert len(requests) == 1 and b"synthetic-panel-secret" in requests[0]
        finally:
            await request_helper.close_panel_client()


def test_unwritable_log_locations_fall_back_to_console_without_shared_temp_file(monkeypatch):
    import utils.logs as logs

    root = logging.Logger("isolated-root")
    original = logging.getLogger
    monkeypatch.setattr(logs.logging, "getLogger", lambda name=None: original(name) if name else root)
    attempted = []

    def unwritable(path, **kwargs):
        attempted.append(path)
        raise PermissionError("synthetic unwritable directory")

    monkeypatch.setattr(logs, "RotatingFileHandler", unwritable)
    monkeypatch.setattr(logs.os.path, "exists", lambda _path: True)
    logs.setup_logging()
    assert root.handlers and isinstance(root.handlers[0], logging.StreamHandler)
    assert all(not path.startswith("/tmp/") for path in attempted)
