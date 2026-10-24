"""Provider contracts for fresh per-IP disable-notice lookups."""

import asyncio

import httpx
import pytest

from utils.isp_detector import ISPDetector


@pytest.mark.parametrize(
    "token,fallback,host",
    [("tok", False, "api.ipinfo.io"), (None, False, "ip-api.com"), ("tok", True, "ip-api.com")],
)
async def test_selected_provider_receives_each_fresh_lookup(token, fallback, host):
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.host == "ip-api.com":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "asname": "Example ISP",
                    "countryCode": "IR",
                    "regionName": "Tehran",
                    "city": "Tehran",
                },
            )
        return httpx.Response(
            200,
            json={
                "ip": "5.6.7.10",
                "asn": "AS123",
                "as_name": "Example ISP",
                "as_domain": "example.net",
                "country_code": "IR",
                "country": "Iran",
                "continent_code": "AS",
                "continent": "Asia",
            },
        )

    detector = ISPDetector(token=token, use_fallback_only=fallback)
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        first = await detector.get_isp_info("5.6.7.10")
        second = await detector.get_isp_info("5.6.7.10")
        assert len(requests) == 2
        assert {request.url.host for request in requests} == {host}
        assert (
            first
            == second
            == {
                "ip": "5.6.7.10",
                "isp": "Example ISP",
                "country": "IR",
                "region": "Unknown" if host == "api.ipinfo.io" else "Tehran",
                "city": "Unknown" if host == "api.ipinfo.io" else "Tehran",
            }
        )
        if host == "api.ipinfo.io":
            assert requests[0].url.path == "/lite/5.6.7.10"
            assert requests[0].headers["Authorization"] == "Bearer tok"
        else:
            assert "Authorization" not in requests[0].headers
    finally:
        await detector.close()


async def test_token_removal_switches_provider_and_discards_previous_auth():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "isp": "Fallback ISP", "countryCode": "IR"})

    detector = ISPDetector(token="old-token")
    old_client = await detector._get_client()
    await detector.reconfigure(token=None)
    assert old_client.is_closed
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        info = await detector.get_isp_info("5.6.7.10")
        assert requests[0].url.host == "ip-api.com"
        assert "Authorization" not in requests[0].headers
        assert info["isp"] == "Fallback ISP"
    finally:
        await detector.close()


@pytest.mark.parametrize("payload", [{}, [], {"as_name": None}, {"as_name": "", "country_code": "IR"}])
async def test_missing_provider_isp_is_explicitly_unknown(payload):
    detector = ISPDetector(token="tok")
    detector._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    try:
        info = await detector.get_isp_info("5.6.7.10")
        assert info["isp"] == "Unknown ISP"
        assert "Unknown ISP" in detector.format_ip_with_isp("5.6.7.10", info)
    finally:
        await detector.close()


@pytest.mark.parametrize(
    "payload,isp,country",
    [
        ({"as_name": "", "as_domain": "example.net", "country_code": "IR", "country": "Iran"}, "example.net", "IR"),
        ({"as_name": None, "as_domain": None, "org": "Legacy ISP", "country": "IR"}, "Legacy ISP", "IR"),
    ],
)
async def test_missing_lite_fields_keep_available_provider_information(payload, isp, country):
    """Missing optional fields retain the provider's available name and country."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    detector = ISPDetector(token="tok")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        info = await detector.get_isp_info("5.6.7.10")
        assert info["isp"] == isp
        assert info["country"] == country
        assert len(requests) == 1
    finally:
        await detector.close()


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_failed_response_does_not_prevent_a_fresh_later_request(status):
    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, json={"error": "unavailable"})
        return httpx.Response(200, json={"as_name": "Recovered ISP", "country_code": "IR"})

    detector = ISPDetector(token="tok")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        assert (await detector.get_isp_info("5.6.7.10"))["isp"] == "Unknown ISP"
        assert (await detector.get_isp_info("5.6.7.10"))["isp"] == "Recovered ISP"
        assert len(requests) == 2
    finally:
        await detector.close()


async def test_notice_deadline_keeps_completed_results_and_marks_slow_ip_unknown():
    async def transport(request):
        if request.url.path == "/lite/5.6.7.20":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"as_name": "Fast ISP", "country_code": "IR"})

    detector = ISPDetector(token="tok")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        results = await detector.get_multiple_isp_info(["5.6.7.10", "5.6.7.20"], timeout=0.02)
        assert results["5.6.7.10"]["isp"] == "Fast ISP"
        assert results["5.6.7.20"]["isp"] == "Unknown ISP"
    finally:
        await detector.close()


async def test_lookup_quotes_user_controlled_url_path():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"as_name": "Example ISP", "country_code": "IR"})

    detector = ISPDetector(token="tok")
    detector._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        await detector.get_isp_info("5.6.7.10?token=other/fragment")
        assert requests[0].url.query == b""
        assert requests[0].url.raw_path == b"/lite/5.6.7.10%3Ftoken%3Dother%2Ffragment"
    finally:
        await detector.close()
