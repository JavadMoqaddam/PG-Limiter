#!/usr/bin/env python3
"""Optional SSE integration check using PANEL_DOMAIN and PANEL_API_KEY."""

import asyncio
import os

import httpx
import pytest

from utils.panel_api import get_nodes, get_panel_headers
from utils.types import PanelType

pytestmark = pytest.mark.integration


async def test_sse_logs():
    """Open an authenticated stream and consume available events for ten seconds."""
    if not os.getenv("PANEL_DOMAIN") or not os.getenv("PANEL_API_KEY"):
        pytest.skip("Set PANEL_DOMAIN and PANEL_API_KEY to run panel integration checks")
    panel_data = PanelType.from_config(
        {"panel": {"domain": os.getenv("PANEL_DOMAIN"), "api_key": os.getenv("PANEL_API_KEY")}}
    )
    nodes = await get_nodes(panel_data, force_refresh=True)
    node = next((node for node in nodes if node.status == "connected"), None)
    assert node is not None, "The configured panel has no connected nodes"

    scheme = "http" if panel_data.panel_domain.startswith("http://") else "https"
    domain = panel_data.panel_domain.removeprefix("https://").removeprefix("http://")
    url = f"{scheme}://{domain}/api/node/{node.node_id}/logs"
    headers = {**get_panel_headers(panel_data), "Accept": "text/event-stream", "Cache-Control": "no-cache"}
    verify_ssl = os.getenv("PANEL_VERIFY_SSL", "false").lower() in ("true", "1", "yes")
    timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
    async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
        async with client.stream("GET", url, headers=headers) as response:
            assert response.status_code == 200, f"Panel SSE returned HTTP {response.status_code}"
            count = 0
            try:
                async with asyncio.timeout(10):
                    async for line in response.aiter_lines():
                        if line.startswith("data: ") and line[6:].strip():
                            count += 1
                        if count >= 5:
                            break
            except TimeoutError:
                pass
            print(f"Authenticated SSE stream received {count} events")


if __name__ == "__main__":
    asyncio.run(test_sse_logs())
