"""Small Mirza transport with conservative, sanitized delivery outcomes."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class MirzaResult:
    status: str
    reason: str
    retry_after: float | None = None


class MirzaClient:
    """Own an independently verified TLS client; never follow credential redirects."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(timeout=10.0, verify=True, follow_redirects=False, transport=transport)

    async def send_message(self, base_url: str, api_key: str, recipient: str, text: str) -> MirzaResult:
        try:
            response = await self._client.post(
                base_url.rstrip("/") + "/users",
                headers={"Token": api_key},
                json={"actions": "send_message", "chat_id": recipient, "text": text},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return MirzaResult("retry", "connection_unavailable")
        except httpx.RequestError:
            return MirzaResult("uncertain", "transport_interrupted")

        if response.status_code == 429:
            try:
                delay = int(response.headers.get("Retry-After", "5"))
            except (TypeError, ValueError):
                delay = 5
            return MirzaResult("retry", "rate_limited", max(1, min(delay, 300)))
        if response.status_code >= 500:
            return MirzaResult("uncertain", "remote_server_error")
        if not 200 <= response.status_code < 300:
            return MirzaResult("failed", "request_rejected")
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            return MirzaResult("uncertain", "invalid_response")
        if isinstance(payload, dict):
            if payload.get("status") is True:
                return MirzaResult("delivered", "acknowledged")
            if payload.get("status") is False:
                return MirzaResult("failed", "message_rejected")
        return MirzaResult("uncertain", "invalid_response")

    async def close(self) -> None:
        await self._client.aclose()
