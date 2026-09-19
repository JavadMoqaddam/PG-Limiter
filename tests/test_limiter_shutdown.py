"""Shutdown must release the ISP detector's shared HTTP client and background tasks."""

import sys
from unittest.mock import AsyncMock


async def test_isp_detector_is_closed(monkeypatch):
    # limiter parses argv at import; give it argv it accepts before importing.
    monkeypatch.setattr(sys, "argv", ["limiter"])
    import limiter
    import utils.check_usage as check_usage_mod

    closed = {"value": False}

    class FakeDetector:
        async def close(self):
            closed["value"] = True

    monkeypatch.setattr(check_usage_mod, "isp_detector", FakeDetector())

    # Neutralize the other cleanup steps so this test isolates the ISP close.
    monkeypatch.setattr(limiter, "close_geo_client", AsyncMock())
    import db.database as db_database
    monkeypatch.setattr(db_database, "close_db", AsyncMock())
    import utils.panel_api.request_helper as request_helper
    monkeypatch.setattr(request_helper, "close_panel_client", AsyncMock())
    import telegram_bot.dispatcher as dispatcher_mod
    monkeypatch.setattr(dispatcher_mod.get_dispatcher(), "stop", AsyncMock())

    await limiter.cleanup_resources()

    assert closed["value"] is True
