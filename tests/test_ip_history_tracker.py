"""The IP-history report must not present a raw unique-IP count over the window as a
device-limit violation: enforcement counts devices (dedup by inbound/subnet/ISP),
so this report is history, not enforcement parity."""

from telegram_bot.handlers import reports as reports_mod
import httpx
import pytest


async def test_report_does_not_claim_device_limit_parity_from_ip_only_rows(monkeypatch):
    from utils.ip_history_tracker import ip_history_tracker

    async def fake_rows(hours, config_data):
        # 3 distinct historical IPs against a limit of 1.
        return [("bob", 3, 1, {"1.1.1.1", "2.2.2.2", "3.3.3.3"})]

    monkeypatch.setattr(ip_history_tracker, "get_users_exceeding_limits", fake_rows)

    report = await ip_history_tracker.generate_report(12, {})
    low = report.lower()

    # No false enforcement-violation claim.
    assert "exceeded limits" not in low
    # Framed as unique IPs over the window, with a note distinguishing IPs from devices.
    assert "unique ip" in low
    assert "device" in low


@pytest.mark.parametrize("hours", [12, 48])
async def test_requested_history_reports_make_no_isp_requests(monkeypatch, hours):
    from utils.ip_history_tracker import ip_history_tracker
    import utils.isp_detector as isp_module

    requests = []
    messages = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "isp": "Example ISP", "countryCode": "IR"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        isp_module.httpx, "AsyncClient", lambda **_: original_client(transport=httpx.MockTransport(transport))
    )

    async def rows(_hours, _config):
        return [("alice", 2, 1, {"5.6.7.10", "5.6.7.20"})]

    async def admin(_update):
        return None

    async def config():
        return {"ipinfo_token": "notice-token"}

    async def send(_update, message, **_kwargs):
        messages.append(message)

    monkeypatch.setattr(ip_history_tracker, "get_users_exceeding_limits", rows)
    monkeypatch.setattr(reports_mod, "check_admin_privilege", admin)
    monkeypatch.setattr(reports_mod, "read_config", config)
    monkeypatch.setattr(reports_mod, "_send_response", send)
    command = reports_mod.ip_history_12h_command if hours == 12 else reports_mod.ip_history_48h_command
    await command(object(), object())

    assert requests == []
    assert "alice" in messages[-1]
    assert "5.6.7.10" in messages[-1] and "5.6.7.20" in messages[-1]
