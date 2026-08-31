#!/usr/bin/env python3
"""
Test script to verify SSE logs functionality

This one talks to a real panel with real credentials, so it is marked
`integration`: conftest.py skips it unless pytest is given --run-integration.
It used to be hidden from CI with --ignore instead, which made the suite look
smaller than it is and left nothing explaining why.
"""

import asyncio
import sys

import pytest

pytestmark = pytest.mark.integration

try:
    import httpx
except ImportError:
    pytest.skip("httpx is not installed", allow_module_level=True)

from utils.panel_api import get_token, get_nodes
from utils.types import PanelType


async def test_sse_logs():
    """Test SSE logs endpoint"""
    
    # Replace with your actual panel credentials
    panel_data = PanelType(
        panel_username="admin",
        panel_password="admin", 
        panel_domain="your-panel-domain.com"
    )
    
    print("Testing SSE logs endpoint...")
    
    try:
        # Get token
        token_result = await get_token(panel_data)
        if isinstance(token_result, ValueError):
            print(f"❌ Token test failed: {token_result}")
            return False
        
        token = token_result.panel_token
        print("✅ Token obtained successfully")
        
        # Get nodes
        nodes_result = await get_nodes(panel_data)
        if isinstance(nodes_result, ValueError):
            print(f"❌ Nodes test failed: {nodes_result}")
            return False
        
        if not nodes_result:
            print("❌ No nodes available")
            return False
        
        # Test SSE connection to first connected node
        connected_node = None
        for node in nodes_result:
            if node.status == "connected":
                connected_node = node
                break
        
        if not connected_node:
            print("❌ No connected nodes found")
            return False
        
        print(f"✅ Testing SSE connection to node: {connected_node.node_name}")
        
        # Determine the scheme
        scheme = "https" if panel_data.panel_domain.startswith("https://") else "https"
        base_url = panel_data.panel_domain.replace("https://", "").replace("http://", "")
        
        url = f"{scheme}://{base_url}/api/node/{connected_node.node_id}/logs"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        
        print(f"Connecting to: {url}")
        
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code != 200:
                        print(f"❌ HTTP {response.status_code}: {response.text}")
                        return False
                    
                    print("✅ SSE connection established successfully")
                    print("Listening for logs for 10 seconds...")
                    
                    # Listen for logs for 10 seconds
                    count = 0
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            log_data = line[6:]
                            if log_data.strip():
                                print(f"📄 Log: {log_data}")
                                count += 1
                        
                        # Stop after 10 seconds or 5 logs
                        if count >= 5:
                            break
                    
                    print(f"✅ Received {count} log entries")
                    return True
                    
            except httpx.TimeoutException:
                print("❌ Connection timed out")
                return False
                
    except Exception as e:
        print(f"❌ Test failed with exception: {e}")
        return False


if __name__ == "__main__":
    print("Testing SSE logs functionality...")
    print("=" * 50)
    
    success = asyncio.run(test_sse_logs())
    
    if success:
        print("\n✅ SSE logs test passed!")
    else:
        print("\n❌ SSE logs test failed!")
        print("Note: Make sure to update the panel credentials in the test script")
