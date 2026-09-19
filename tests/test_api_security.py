"""API security defaults: loopback bind and throttled failed authentication."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

import api_server


def test_default_bind_loopback(monkeypatch):
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.setattr(api_server, "load_config", lambda: {})
    assert api_server._resolve_bind_host() == "127.0.0.1"


def test_failed_auth_throttled(monkeypatch):
    monkeypatch.delenv("API_USERNAME", raising=False)
    monkeypatch.delenv("API_PASSWORD", raising=False)
    monkeypatch.setattr(
        api_server, "load_config", lambda: {"api": {"username": "admin", "password": "secret"}}
    )
    monkeypatch.setattr(api_server, "_failed_auth_attempts", {})

    request = MagicMock()
    request.client.host = "203.0.113.7"
    bad = HTTPBasicCredentials(username="admin", password="wrong")

    # Each wrong attempt up to the threshold is a plain 401.
    for _ in range(api_server._AUTH_MAX_FAILURES):
        with pytest.raises(HTTPException) as exc:
            api_server.verify_credentials(request=request, credentials=bad)
        assert exc.value.status_code == 401

    # Once the threshold is exceeded the client is throttled with 429.
    with pytest.raises(HTTPException) as exc:
        api_server.verify_credentials(request=request, credentials=bad)
    assert exc.value.status_code == 429
