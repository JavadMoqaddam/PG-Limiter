"""The IP-history report must not present a raw unique-IP count over the window as a
device-limit violation: enforcement counts devices (dedup by inbound/subnet/ISP),
so this report is history, not enforcement parity."""

from telegram_bot.handlers import reports as reports_mod


async def test_report_does_not_claim_device_limit_parity_from_ip_only_rows(monkeypatch):
    from utils.ip_history_tracker import ip_history_tracker

    async def fake_rows(hours, config_data):
        # 3 distinct historical IPs against a limit of 1.
        return [("bob", 3, 1, {"1.1.1.1", "2.2.2.2", "3.3.3.3"})]

    monkeypatch.setattr(ip_history_tracker, "get_users_exceeding_limits", fake_rows)

    report = await ip_history_tracker.generate_report(12, {}, None)
    low = report.lower()

    # No false enforcement-violation claim.
    assert "exceeded limits" not in low
    # Framed as unique IPs over the window, with a note distinguishing IPs from devices.
    assert "unique ip" in low
    assert "device" in low


def test_report_isp_detector_reads_canonical_config_keys():
    detector = reports_mod._build_report_isp_detector({"ipinfo_token": "tok123"})
    assert detector is not None
    assert detector.token == "tok123"

    fallback = reports_mod._build_report_isp_detector({"api": {"use_fallback_isp_api": True}})
    assert fallback is not None
    assert fallback.use_fallback_only is True

    # The old uppercase keys are not part of the canonical config and must be ignored.
    none_detector = reports_mod._build_report_isp_detector(
        {"IPINFO_TOKEN": "x", "USE_FALLBACK_ISP_API": True}
    )
    assert none_detector is None
