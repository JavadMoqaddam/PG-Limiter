"""Fresh ISP lookups for successful disable notifications.

Each displayed IP is queried separately. Lookup results are never cached or
aggregated by subnet because only applied disables need ISP information.
"""

import asyncio
from urllib.parse import quote

import httpx

from utils.logs import logger


def _default_isp_info(ip: str) -> dict[str, str]:
    """Return explicit unknown information when the provider cannot identify an IP."""
    return {"ip": ip, "isp": "Unknown ISP", "country": "Unknown", "city": "Unknown", "region": "Unknown"}


class ISPDetector:
    """Own the shared HTTP client used for fresh per-IP disable-notice lookups."""

    def __init__(self, token: str | None = None, use_fallback_only: bool = False):
        self.token = token or None
        self._configured_fallback = bool(use_fallback_only)
        self.use_fallback_only = self._configured_fallback or not self.token
        self._client: httpx.AsyncClient | None = None

    @property
    def config_signature(self) -> tuple[str | None, bool]:
        """Expose configured provider settings for live token changes."""
        return self.token, self._configured_fallback

    async def reconfigure(self, token: str | None, fallback: bool = False) -> None:
        """Apply current settings, including token removal, and replace the HTTP client."""
        await self.close()
        self.token = token or None
        self._configured_fallback = bool(fallback)
        self.use_fallback_only = self._configured_fallback or not self.token

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5), timeout=4.0
            )
        return self._client

    async def close(self) -> None:
        """Release the shared HTTP client on shutdown or a provider settings change."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_isp_info(self, ip: str) -> dict[str, str]:
        """Query one IP freshly, returning unknown information on provider failure."""
        headers = {}
        if self.use_fallback_only:
            url = (
                f"http://ip-api.com/json/{quote(ip, safe=':')}?fields=status,countryCode,regionName,city,isp,org,asname"
            )
        else:
            url = f"https://ipinfo.io/{quote(ip, safe=':')}/json"
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            client = await self._get_client()
            response = await client.get(url, headers=headers, timeout=4.0)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return _default_isp_info(ip)
            if self.use_fallback_only:
                if data.get("status") != "success":
                    return _default_isp_info(ip)
                isp = data.get("asname") or data.get("isp") or data.get("org")
                country = data.get("countryCode")
                region = data.get("regionName")
            else:
                isp = data.get("as_domain") or data.get("as_name") or data.get("org")
                country = data.get("country")
                region = data.get("region")
            return {
                "ip": ip,
                "isp": str(isp or "Unknown ISP"),
                "country": str(country or "Unknown"),
                "city": str(data.get("city") or "Unknown"),
                "region": str(region or "Unknown"),
            }
        except Exception as error:
            logger.warning("ISP lookup failed for %s: %s", ip, type(error).__name__)
            return _default_isp_info(ip)

    async def get_multiple_isp_info(self, ips: list[str], timeout: float = 8.0) -> dict[str, dict[str, str]]:
        """Query every raw IP with bounded concurrency and a total notification deadline."""
        results = {ip: _default_isp_info(ip) for ip in ips}
        semaphore = asyncio.Semaphore(3)

        async def lookup(ip: str) -> None:
            async with semaphore:
                results[ip] = await self.get_isp_info(ip)

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*(lookup(ip) for ip in ips))
        except TimeoutError:
            logger.warning("ISP lookups exceeded the disable-notification deadline (%ss)", timeout)
        return results

    @staticmethod
    def format_ip_with_isp(ip: str, isp_info: dict[str, str]) -> str:
        """Render an IP with its ISP, explicitly retaining unknown lookup results."""
        isp = isp_info.get("isp") or "Unknown ISP"
        country = isp_info.get("country") or "Unknown"
        if isp.startswith("AS") and " " in isp:
            isp = isp.split(" ", 1)[1]
        return f"{ip} ({isp}, {country})"
