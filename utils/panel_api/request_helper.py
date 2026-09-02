"""
Panel API Request Helper with Fallback and Retry

This module provides a unified way to make panel API requests with:
- Automatic retry with exponential backoff
- Fallback between HTTPS and HTTP
- Token refresh on 401 errors
- Proper error logging
"""

import asyncio
import os
import random
import time
from ssl import SSLError
from typing import Optional, Any, Literal

import httpx

from utils.logs import log_api_request, get_logger
from utils.types import PanelType
from utils.panel_api.auth import get_token, invalidate_token_cache

# Module logger
request_logger = get_logger("panel_api.request")


class PanelCircuitBreaker:
    """
    Circuit Breaker pattern for Panel API to prevent thundering herd
    and system hanging during panel outages.
    
    States:
    - CLOSED: Normal operation, requests flow freely.
    - OPEN: Consecutive server/network failures (5+) tripped breaker. Fail-fast for cooldown.
    - HALF_OPEN: Cooldown expired, testing 1 request.
    """
    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"
    
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = self.STATE_CLOSED
        self.consecutive_failures = 0
        self.last_state_change = time.time()
        
    def allow_request(self) -> bool:
        """Check if request is allowed by circuit breaker state."""
        now = time.time()
        if self.state == self.STATE_CLOSED:
            return True
        elif self.state == self.STATE_OPEN:
            if now - self.last_state_change > self.recovery_timeout:
                self.state = self.STATE_HALF_OPEN
                self.last_state_change = now
                request_logger.info("🟡 Circuit Breaker enters HALF_OPEN state: Testing Panel API connectivity...")
                return True
            return False
        elif self.state == self.STATE_HALF_OPEN:
            return True
        return True

    def record_result(self, is_server_failure: bool):
        """Record success or server/network failure (ignoring 4xx client errors)."""
        now = time.time()
        if is_server_failure:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold and self.state != self.STATE_OPEN:
                self.state = self.STATE_OPEN
                self.last_state_change = now
                request_logger.warning(f"⚡ Circuit Breaker TRIPPED to OPEN state! Consecutive failures: {self.consecutive_failures}. Cooldown: {self.recovery_timeout}s")
        else:
            # Success or 4xx client error (panel server is reachable and functioning)
            if self.state != self.STATE_CLOSED:
                request_logger.info(f"✅ Circuit Breaker RESET to CLOSED state after successful response.")
            self.consecutive_failures = 0
            self.state = self.STATE_CLOSED


# Shared circuit breaker instance
circuit_breaker = PanelCircuitBreaker(failure_threshold=5, recovery_timeout=60.0)

# Shared persistent httpx.AsyncClient with connection pooling
_panel_client: Optional[httpx.AsyncClient] = None


async def get_panel_client() -> httpx.AsyncClient:
    """
    Get or create shared persistent httpx.AsyncClient for Panel API requests
    with connection pooling, keepalive, and proper connection limits.
    """
    global _panel_client
    if _panel_client is None or _panel_client.is_closed:
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)
        verify_ssl = os.getenv("PANEL_VERIFY_SSL", "false").lower() in ("true", "1", "yes")
        _panel_client = httpx.AsyncClient(
            verify=verify_ssl,
            timeout=30.0,
            limits=limits,
        )
    return _panel_client


async def close_panel_client():
    """Close the shared Panel API httpx.AsyncClient if open."""
    global _panel_client
    if _panel_client is not None:
        try:
            if not getattr(_panel_client, "is_closed", False):
                res = _panel_client.aclose()
                if asyncio.iscoroutine(res):
                    await res
            request_logger.debug("🔒 Closed shared Panel API HTTP client")
        except Exception as e:
            request_logger.warning(f"Error closing panel client: {e}")
        finally:
            _panel_client = None


# Track panel endpoint health
_panel_health = {
    "https_failures": 0,
    "http_failures": 0,
    "last_https_success": 0,
    "last_http_success": 0,
    "prefer_https": True,  # Start with HTTPS preference
    "panel_available": True,  # Track if panel is available
    "last_unavailable_time": 0,  # When panel became unavailable
    "consecutive_failures": 0,  # Count consecutive connection failures
}

# Panel availability settings
PANEL_UNAVAILABLE_THRESHOLD = 5  # Failures before marking panel as unavailable
PANEL_CHECK_INTERVAL = 10  # Seconds between availability checks when panel is down
MAX_WAIT_FOR_PANEL = 600  # Maximum seconds to wait for panel (10 minutes)


def _get_scheme_order(domain: str = "") -> list[str]:
    """
    Schemes to try for the configured panel domain, in order.

    An explicit prefix is honoured; anything else (including the scheme-less
    ``host:port`` form used in .env.example) means HTTPS only. There used to be a
    trailing ``["https", "http"]`` fallback, but the test above it made it
    unreachable - and reaching it would have been worse than the bug, since it would
    retry the admin login over plaintext HTTP after an HTTPS failure.
    """
    if domain.startswith("http://"):
        return ["http"]
    return ["https"]


def _record_success(scheme: str):
    """Record a successful request."""
    _panel_health[f"{scheme}_failures"] = 0
    _panel_health[f"last_{scheme}_success"] = time.time()


def _record_failure(scheme: str):
    """Record a failed request."""
    _panel_health[f"{scheme}_failures"] = _panel_health.get(f"{scheme}_failures", 0) + 1


def _record_connection_failure():
    """Record a connection failure (panel might be restarting)."""
    _panel_health["consecutive_failures"] += 1
    if _panel_health["consecutive_failures"] >= PANEL_UNAVAILABLE_THRESHOLD:
        if _panel_health["panel_available"]:
            _panel_health["panel_available"] = False
            _panel_health["last_unavailable_time"] = time.time()
            request_logger.warning("⚠️ Panel appears to be unavailable (restarting?)")


def _record_connection_success():
    """Record a successful connection (panel is available)."""
    was_unavailable = not _panel_health["panel_available"]
    _panel_health["consecutive_failures"] = 0
    _panel_health["panel_available"] = True
    if was_unavailable:
        downtime = time.time() - _panel_health["last_unavailable_time"]
        request_logger.info(f"✅ Panel is back online after {downtime:.0f}s")


async def check_panel_availability(panel_data: PanelType, timeout: float = 5.0) -> bool:
    """
    Quick check if the panel is available.
    
    Args:
        panel_data: Panel connection data
        timeout: Timeout for the check in seconds
        
    Returns:
        bool: True if panel is reachable, False otherwise
    """
    client = await get_panel_client()
    # Pass the domain: calling this with no argument always yielded HTTPS, so an
    # http:// panel could never be probed. Strip any prefix too, or the URL below
    # would come out as "https://http://host/api/".
    bare_domain = panel_data.panel_domain
    for prefix in ("https://", "http://"):
        if bare_domain.startswith(prefix):
            bare_domain = bare_domain[len(prefix):]
            break
    for scheme in _get_scheme_order(panel_data.panel_domain):
        url = f"{scheme}://{bare_domain}/api/"
        try:
            response = await client.get(url, timeout=timeout)
            # Any response (even 401/404) means panel is up
            if response.status_code < 500:
                _record_connection_success()
                return True
        except (httpx.ConnectError, httpx.TimeoutException, SSLError):
            continue
        except Exception:
            continue
    
    _record_connection_failure()
    return False


async def wait_for_panel(panel_data: PanelType, max_wait: float = None) -> bool:
    """
    Wait for the panel to become available (useful during panel restarts).
    
    Args:
        panel_data: Panel connection data
        max_wait: Maximum seconds to wait (default: MAX_WAIT_FOR_PANEL)
        
    Returns:
        bool: True if panel became available, False if timeout
    """
    if max_wait is None:
        max_wait = MAX_WAIT_FOR_PANEL
    
    start_time = time.time()
    check_count = 0
    
    while (time.time() - start_time) < max_wait:
        check_count += 1
        if await check_panel_availability(panel_data):
            if check_count > 1:
                elapsed = time.time() - start_time
                request_logger.info(f"✅ Panel available after {elapsed:.0f}s ({check_count} checks)")
            return True
        
        elapsed = time.time() - start_time
        remaining = max_wait - elapsed
        wait_time = min(PANEL_CHECK_INTERVAL, remaining)
        
        if check_count == 1:
            request_logger.warning(f"⏳ Panel unavailable, waiting up to {max_wait:.0f}s for it to come back...")
        elif check_count % 6 == 0:  # Log every ~60 seconds
            request_logger.info(f"⏳ Still waiting for panel... ({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)")
        
        if wait_time > 0:
            await asyncio.sleep(wait_time)
    
    request_logger.error(f"❌ Panel did not become available after {max_wait:.0f}s")
    return False


async def panel_request(
    panel_data: PanelType,
    method: Literal["GET", "POST", "PUT", "DELETE"],
    endpoint: str,
    token: str,
    params: Optional[dict] = None,
    json_data: Optional[dict] = None,
    form_data: Optional[dict] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> tuple[Optional[httpx.Response], Optional[str]]:
    """
    Make a panel API request with automatic retry and scheme fallback.
    
    Args:
        panel_data: Panel connection data
        method: HTTP method
        endpoint: API endpoint (e.g., "/api/users")
        token: Bearer token for authorization
        params: Query parameters for GET/POST/etc. requests
        json_data: JSON body for POST/PUT requests
        form_data: Form data for POST requests
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts
        retry_delay: Initial delay between retries (doubles each retry)
    
    Returns:
        Tuple of (response, error_message)
        - On success: (response, None)
        - On failure: (None, error_message)
    """
    # Check Circuit Breaker before making network requests
    if not circuit_breaker.allow_request():
        request_logger.warning(f"⚡ Circuit Breaker OPEN: Panel API unavailable, failing fast for {endpoint}")
        return None, "Circuit Breaker OPEN: Panel API unavailable"

    headers = {"Authorization": f"Bearer {token}"}
    last_error = None
    
    for attempt in range(max_retries):
        schemes = _get_scheme_order(panel_data.panel_domain)
        clean_domain = panel_data.panel_domain
        if clean_domain.startswith("https://"):
            clean_domain = clean_domain[8:]
        elif clean_domain.startswith("http://"):
            clean_domain = clean_domain[7:]
        
        for scheme in schemes:
            url = f"{scheme}://{clean_domain}{endpoint}"
            start_time = time.perf_counter()
            
            try:
                client = await get_panel_client()
                if method == "GET":
                    response = await client.get(url, headers=headers, params=params, timeout=timeout)
                elif method == "POST":
                    if form_data:
                        response = await client.post(url, headers=headers, data=form_data, params=params, timeout=timeout)
                    else:
                        response = await client.post(url, headers=headers, json=json_data, params=params, timeout=timeout)
                elif method == "PUT":
                    response = await client.put(url, headers=headers, json=json_data, params=params, timeout=timeout)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers, params=params, timeout=timeout)
                else:
                    response = await client.request(method, url, headers=headers, json=json_data, params=params, timeout=timeout)
                
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request(method, url, response.status_code, elapsed)
                
                # Any response means panel server is reachable
                _record_connection_success()
                
                # 4xx Client Errors (400, 401, 403, 404): Panel is UP and functioning!
                # Reset/don't trip Circuit Breaker on 4xx business responses
                if response.status_code < 500:
                    circuit_breaker.record_result(is_server_failure=False)
                
                # Success
                if response.status_code in (200, 201, 204):
                    _record_success(scheme)
                    return response, None
                
                # Auth error - caller should refresh token
                if response.status_code == 401:
                    _record_failure(scheme)
                    return response, "Unauthorized - token may be expired"
                
                # Not found
                if response.status_code == 404:
                    _record_success(scheme)  # Server responded, just not found
                    return response, None
                
                # Rate limited (429)
                if response.status_code == 429:
                    _record_failure(scheme)
                    last_error = f"Rate limited (429) on {url}"
                    request_logger.warning(last_error)
                    await asyncio.sleep(retry_delay * 2 + random.uniform(0.1, 0.5))
                    continue
                
                # 5xx Server error - count as server failure for Circuit Breaker
                if response.status_code >= 500:
                    circuit_breaker.record_result(is_server_failure=True)
                    _record_failure(scheme)
                    last_error = f"Server error ({response.status_code}) on {url}"
                    request_logger.warning(last_error)
                    continue
                
                # Other errors
                _record_failure(scheme)
                last_error = f"HTTP {response.status_code}: {response.text[:100]}"
                    
            except (SSLError, httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as e:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request(method, url, None, elapsed, type(e).__name__)
                _record_failure(scheme)
                _record_connection_failure()
                # Server/network failure - trip Circuit Breaker
                circuit_breaker.record_result(is_server_failure=True)
                last_error = f"Network/Server error on {url}: {type(e).__name__}: {str(e)[:50]}"
                request_logger.warning(last_error)
                continue
                
            except Exception as e:
                elapsed = (time.perf_counter() - start_time) * 1000
                log_api_request(method, url, None, elapsed, str(e)[:50])
                _record_failure(scheme)
                circuit_breaker.record_result(is_server_failure=True)
                last_error = f"Error on {url}: {type(e).__name__}: {str(e)[:50]}"
                request_logger.error(last_error)
                continue
        
        # All schemes failed for this attempt, wait before retry with proportional jitter
        if attempt < max_retries - 1:
            base_delay = min(10.0, retry_delay * (2 ** attempt))
            jitter = random.uniform(0, base_delay * 0.5)
            wait_time = base_delay + jitter
            request_logger.debug(f"Retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait_time)
    
    return None, last_error or "All attempts failed"


async def _get_token_for_request(panel_data: PanelType, force_refresh: bool = False) -> Optional[str]:
    """Get a valid token for making requests."""
    try:
        token_result = await get_token(panel_data, force_refresh=force_refresh)
        if isinstance(token_result, ValueError):
            request_logger.error(f"Failed to get token: {token_result}")
            return None
        return token_result.panel_token
    except Exception as e:
        request_logger.error(f"Token acquisition error: {e}")
        return None


async def _execute_panel_request(
    panel_data: PanelType,
    method: Literal["GET", "POST", "PUT", "DELETE"],
    endpoint: str,
    force_refresh: bool = False,
    json_data: Optional[dict] = None,
    form_data: Optional[dict] = None,
    params: Optional[dict] = None,
    **kwargs
) -> Optional[httpx.Response]:
    """Internal helper executing panel requests with automatic token handling and 401 retry."""
    token = await _get_token_for_request(panel_data, force_refresh)
    if not token:
        return None
    
    response, error = await panel_request(
        panel_data, method, endpoint, token,
        params=params, json_data=json_data, form_data=form_data, **kwargs
    )
    
    # On 401, retry once with fresh token
    if response and response.status_code == 401 and not force_refresh:
        await invalidate_token_cache()
        token = await _get_token_for_request(panel_data, force_refresh=True)
        if token:
            response, error = await panel_request(
                panel_data, method, endpoint, token,
                params=params, json_data=json_data, form_data=form_data, **kwargs
            )
    
    return response


async def panel_get(
    panel_data: PanelType,
    endpoint: str,
    force_refresh: bool = False,
    **kwargs
) -> Optional[httpx.Response]:
    """Convenience wrapper for GET requests with automatic token handling."""
    return await _execute_panel_request(panel_data, "GET", endpoint, force_refresh=force_refresh, **kwargs)


async def panel_post(
    panel_data: PanelType,
    endpoint: str,
    json_data: Optional[dict] = None,
    form_data: Optional[dict] = None,
    force_refresh: bool = False,
    **kwargs
) -> Optional[httpx.Response]:
    """Convenience wrapper for POST requests with automatic token handling."""
    return await _execute_panel_request(
        panel_data, "POST", endpoint, force_refresh=force_refresh,
        json_data=json_data, form_data=form_data, **kwargs
    )


async def panel_put(
    panel_data: PanelType,
    endpoint: str,
    json_data: Optional[dict] = None,
    force_refresh: bool = False,
    **kwargs
) -> Optional[httpx.Response]:
    """Convenience wrapper for PUT requests with automatic token handling."""
    return await _execute_panel_request(
        panel_data, "PUT", endpoint, force_refresh=force_refresh,
        json_data=json_data, **kwargs
    )


async def panel_delete(
    panel_data: PanelType,
    endpoint: str,
    force_refresh: bool = False,
    **kwargs
) -> Optional[httpx.Response]:
    """Convenience wrapper for DELETE requests with automatic token handling."""
    return await _execute_panel_request(panel_data, "DELETE", endpoint, force_refresh=force_refresh, **kwargs)


def get_panel_health() -> dict:
    """Get current panel endpoint health status."""
    return {
        "https": {
            "failures": _panel_health["https_failures"],
            "last_success": _panel_health["last_https_success"],
        },
        "http": {
            "failures": _panel_health["http_failures"],
            "last_success": _panel_health["last_http_success"],
        },
        "preferred_scheme": _get_scheme_order()[0],
        "panel_available": _panel_health["panel_available"],
        "consecutive_failures": _panel_health["consecutive_failures"],
        "last_unavailable_time": _panel_health["last_unavailable_time"],
    }


def reset_panel_health():
    """Reset panel health tracking."""
    global _panel_health
    _panel_health = {
        "https_failures": 0,
        "http_failures": 0,
        "last_https_success": 0,
        "last_http_success": 0,
        "prefer_https": True,
        "panel_available": True,
        "last_unavailable_time": 0,
        "consecutive_failures": 0,
    }


def is_panel_available() -> bool:
    """Check if panel is currently marked as available."""
    return _panel_health["panel_available"]
