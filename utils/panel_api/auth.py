"""
Authentication and token management for panel API.
"""

import asyncio
import random
import time
from ssl import SSLError

try:
    import httpx
except ImportError as exc:
    raise ImportError("Module 'httpx' is not installed. Use: 'pip install httpx'") from exc

from utils.logs import logger, log_api_request, get_logger
from utils.types import PanelType

# Module logger
auth_logger = get_logger("panel_api.auth")

# In-process token cache (the token is per-process, so nothing is gained by
# sharing it through an external store)
_token_cache = {
    "token": None,
    "expires_at": 0,
    "panel_domain": None
}


async def invalidate_token_cache():
    """Invalidate the cached token (useful when getting 401 errors)"""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0
    auth_logger.info("🔑 Token cache invalidated")


async def safe_send_logs_panel(message: str):
    """Safely send logs from panel_api, handling import errors gracefully"""
    try:
        from telegram_bot.send_message import send_logs
        await send_logs(message)
    except ImportError as e:
        auth_logger.warning(f"Could not import send_logs: {e}")
    except Exception as e:
        auth_logger.error(f"Failed to send telegram message: {e}")


_auth_token_lock = asyncio.Lock()


async def get_token(panel_data: PanelType, force_refresh: bool = False) -> PanelType:
    """
    Get access token from the panel API with caching (Redis or in-memory).
    Tokens are cached for 30 minutes to minimize unnecessary API calls.
    Uses asyncio.Lock to prevent thundering herd API requests under concurrency.
    
    Args:
        panel_data (PanelType): A PanelType object containing
        the username, password, and domain for the panel API.
        force_refresh (bool): Force getting a new token even if cached one exists.

    Returns:
        PanelType: The panel data with access token set.

    Raises:
        ValueError: If the function fails to get a token from both the HTTP
        and HTTPS endpoints.
    """
    current_time = time.time()

    # Fast path: Check in-memory cache without lock if not force_refresh
    if (not force_refresh and
        _token_cache["token"] is not None and
        _token_cache["panel_domain"] == panel_data.panel_domain and
        current_time < _token_cache["expires_at"]):
        panel_data.panel_token = _token_cache["token"]
        remaining = int(_token_cache["expires_at"] - current_time)
        auth_logger.debug(f"🔑 Using in-memory cached token (expires in {remaining}s)")
        return panel_data

    # Acquire lock for fetching/refreshing token to prevent thundering herd
    async with _auth_token_lock:
        current_time = time.time()

        # Double-check cache inside lock (in case another task fetched the token while we waited for lock)
        if (not force_refresh and
            _token_cache["token"] is not None and
            _token_cache["panel_domain"] == panel_data.panel_domain and
            current_time < _token_cache["expires_at"]):
            panel_data.panel_token = _token_cache["token"]
            auth_logger.debug(f"🔑 Using in-memory cached token (resolved after lock)")
            return panel_data

        auth_logger.info(f"🔑 Fetching new token for {panel_data.panel_domain} (force_refresh={force_refresh})")
    
    # Need to fetch a new token
    payload = {
        "username": f"{panel_data.panel_username}",
        "password": f"{panel_data.panel_password}",
    }
    
    from utils.panel_api.request_helper import _get_scheme_order, get_panel_client
    schemes = _get_scheme_order(panel_data.panel_domain)
    clean_domain = panel_data.panel_domain
    if clean_domain.startswith("https://"):
        clean_domain = clean_domain[8:]
    elif clean_domain.startswith("http://"):
        clean_domain = clean_domain[7:]

    max_attempts = 5
    for attempt in range(max_attempts):
        auth_logger.debug(f"🔑 Token fetch attempt {attempt + 1}/{max_attempts}")
        for scheme in schemes:
            url = f"{scheme}://{clean_domain}/api/admin/token"
            start_time = time.perf_counter()
            try:
                from utils.panel_api.request_helper import get_panel_client
                client = await get_panel_client()
                response = await client.post(url, data=payload, timeout=10.0)
                elapsed = (time.perf_counter() - start_time) * 1000
                response.raise_for_status()
                
                log_api_request("POST", url, response.status_code, elapsed)
                
                # Try to parse JSON response
                try:
                    json_obj = response.json()
                except Exception as json_error:
                    auth_logger.error(f"Failed to parse JSON from {url}: {json_error}")
                    auth_logger.debug(f"Response text: {response.text[:200]}")
                    continue
                
                # Check if response is a dict and has access_token
                if not isinstance(json_obj, dict):
                    auth_logger.error(f"Response is not a dict: {type(json_obj)} - {json_obj}")
                    continue
                
                if "access_token" not in json_obj:
                    auth_logger.error(f"Response missing 'access_token' key. Keys: {list(json_obj.keys())}")
                    continue
                    
                token = json_obj["access_token"]

                # Cache the token for 30 minutes (1800 seconds)
                _token_cache["token"] = token
                _token_cache["expires_at"] = current_time + 1800
                _token_cache["panel_domain"] = panel_data.panel_domain
                
                panel_data.panel_token = token
                auth_logger.info(f"🔑 Token obtained successfully (cached for 30 minutes) [{elapsed:.0f}ms]")
                return panel_data
            except httpx.HTTPStatusError:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request("POST", url, response.status_code, elapsed, f"HTTP {response.status_code}")
                message = f"[{response.status_code}] {response.text}"
                await safe_send_logs_panel(message)
                auth_logger.error(f"HTTP error: {message}")
                continue
            except SSLError as ssl_err:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request("POST", url, None, elapsed, f"SSL Error: {ssl_err}")
                auth_logger.debug(f"SSL error for {scheme}, trying next scheme")
                continue
            except httpx.TimeoutException:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request("POST", url, None, elapsed, "Timeout")
                auth_logger.warning(f"Timeout connecting to {url}")
                continue
            except httpx.ConnectError as conn_err:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request("POST", url, None, elapsed, f"Connection error: {conn_err}")
                auth_logger.warning(f"Connection error to {url}: {conn_err}")
                continue
            except Exception as error:  # pylint: disable=broad-except
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request("POST", url, None, elapsed, str(error))
                message = f"An unexpected error occurred: {error}"
                await safe_send_logs_panel(message)
                auth_logger.error(message)
                continue
        wait_time = min(30, random.randint(2, 5) * (attempt + 1))
        auth_logger.debug(f"🔑 Waiting {wait_time}s before retry...")
        await asyncio.sleep(wait_time)
    
    message = (
        f"Failed to get token after {max_attempts} attempts. Make sure the panel is running "
        + "and the username and password are correct."
    )
    await safe_send_logs_panel(message)
    auth_logger.error(f"🔑 {message}")
    raise ValueError(message)
