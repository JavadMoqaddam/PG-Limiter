"""Country filtering must not spend ipinfo requests reserved for disable reports."""

import httpx

import utils.parse_logs as parse_logs


async def test_country_fallback_does_not_query_ipinfo(monkeypatch):
    hosts = []

    def respond(request):
        hosts.append(request.url.host)
        if request.url.host == "ip-api.com":
            return httpx.Response(503)
        if request.url.host == "ipinfo.io":
            return httpx.Response(200, json={"country": "DE"})
        return httpx.Response(200, json={"country_code2": "DE"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

        async def get_client():
            return client

        monkeypatch.setattr(parse_logs, "get_geo_client", get_client)
        monkeypatch.setattr(parse_logs, "_endpoint_failures", {})
        monkeypatch.setattr(parse_logs, "_endpoint_last_success", {})
        monkeypatch.setattr(parse_logs, "COUNTRY_CACHE", {})
        assert await parse_logs.lookup_country("49.12.186.89") == "DE"
    assert hosts == ["ip-api.com", "api.iplocation.net"]
