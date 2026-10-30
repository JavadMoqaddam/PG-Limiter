#!/usr/bin/env python3
"""Optional integration checks using PANEL_DOMAIN and PANEL_API_KEY."""

import asyncio
import os

import pytest

from utils.panel_api import all_user, get_nodes
from utils.types import PanelType

pytestmark = pytest.mark.integration


async def test_new_api():
    """Verify the users and nodes endpoints against the configured panel."""
    if not os.getenv("PANEL_DOMAIN") or not os.getenv("PANEL_API_KEY"):
        pytest.skip("Set PANEL_DOMAIN and PANEL_API_KEY to run panel integration checks")
    panel_data = PanelType.from_config(
        {"panel": {"domain": os.getenv("PANEL_DOMAIN"), "api_key": os.getenv("PANEL_API_KEY")}}
    )
    users = await all_user(panel_data)
    nodes = await get_nodes(panel_data, force_refresh=True)

    assert isinstance(users, list)
    assert isinstance(nodes, list)
    print(f"Panel API connected: {len(users)} users and {len(nodes)} nodes")


if __name__ == "__main__":
    asyncio.run(test_new_api())
