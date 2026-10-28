"""
This module contains functions to parse and validate logs.
"""

import re
import time

from cachetools import TTLCache

from utils.shared_state import ACTIVE_USERS, ACTIVE_USERS_LOCK
from utils.ip_facts import (
    BLOCKED_IPS,
    COUNTRY_CACHE,
    VERDICT_CACHE,
    is_ip_accepted,
    is_public_ip,
)
from utils.read_config import read_config, get_config_revision
from utils.log_observations import LOG_OBSERVATIONS, FutureTimestampError
from utils.monitoring_gate import monitoring_is_current, monitoring_snapshot, synchronize_monitoring
from utils.types import ConnectionInfo, DeviceInfo, UserType
from utils.logs import get_logger

parse_logger = get_logger("parse_logs")

try:
    import httpx
except ImportError as exc:
    raise ImportError("Module 'httpx' is not installed. Use: 'pip install httpx'") from exc

INVALID_EMAILS = {
    "API]",
    "Found",
    "(normal)",
    "timeout",
    "EOF",
    "address",
    "INFO",
    "request",
}

# Kept as module-level names for the callers and tests that still import them.
# ``INVALID_IPS`` is the node/resolver blocklist and ``VALID_IPS`` the verdict
# cache; both live in utils.ip_facts now.
INVALID_IPS = BLOCKED_IPS
VALID_IPS = VERDICT_CACHE

LOCAL_ID_CACHE = TTLCache(maxsize=30000, ttl=600)

# API endpoints with fallback order (from most reliable to least)
API_ENDPOINTS = [
    {"url": "http://ip-api.com/json/{ip}?fields=countryCode", "key": "countryCode", "name": "ip-api.com"},
    {"url": "https://api.iplocation.net/?ip={ip}", "key": "country_code2", "name": "iplocation.net"},
    {"url": "https://ipapi.co/{ip}/country", "key": None, "name": "ipapi.co"},
]

# Track failed endpoints to prioritize working ones
_endpoint_failures = {endpoint["name"]: 0 for endpoint in API_ENDPOINTS}
_endpoint_last_success = {endpoint["name"]: 0 for endpoint in API_ENDPOINTS}


async def remove_id_from_username(username: str) -> str:
    """
    Remove the ID from the start of the username.
    Args:
        username (str): The username string from which to remove the ID.

    Returns:
        str: The username with the ID removed.
    """
    return re.sub(r"^\d+\.", "", username)


_geo_client: httpx.AsyncClient | None = None


async def get_geo_client() -> httpx.AsyncClient:
    """Get or create shared persistent httpx.AsyncClient for GeoIP lookups with connection pooling."""
    global _geo_client
    if _geo_client is None or _geo_client.is_closed:
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0)
        # These GeoIP providers are public, CA-signed HTTPS hosts, so certificate
        # verification is safe to enable (unlike a possibly self-signed panel).
        _geo_client = httpx.AsyncClient(verify=True, timeout=5.0, limits=limits)
    return _geo_client


async def close_geo_client() -> None:
    """Close the shared GeoIP httpx.AsyncClient if open."""
    global _geo_client
    if _geo_client is not None and not _geo_client.is_closed:
        await _geo_client.aclose()
        _geo_client = None


async def lookup_country(ip_address: str) -> None | str:
    """
    Resolve the country of an IP through the geo providers, in reliability order.

    Callers should go through ``utils.ip_facts.resolve_country`` so the result is
    cached; this function performs the actual network lookup.

    Args:
        ip_address (str): The IP address to check.

    Returns:
        str: The country code of the IP address location, or None
    """
    global _endpoint_failures, _endpoint_last_success

    # Sort endpoints by reliability (fewer failures first, recent success preferred)
    current_time = time.time()
    sorted_endpoints = sorted(
        API_ENDPOINTS,
        key=lambda e: (
            _endpoint_failures.get(e["name"], 0),
            -(current_time - _endpoint_last_success.get(e["name"], 0)),
        ),
    )

    last_error = None
    client = await get_geo_client()
    for endpoint in sorted_endpoints:
        url = endpoint["url"].format(ip=ip_address)
        key = endpoint["key"]
        name = endpoint["name"]

        try:
            resp = await client.get(url, timeout=3)

            if resp.status_code == 200:
                if key is None:
                    # Direct text response (like ipapi.co/country)
                    country = resp.text.strip()
                else:
                    info = resp.json()
                    country = info.get(key)

                if country and len(country) == 2:  # Valid country code
                    COUNTRY_CACHE[ip_address] = country
                    _endpoint_failures[name] = max(0, _endpoint_failures.get(name, 0) - 1)
                    _endpoint_last_success[name] = current_time
                    return country
            elif resp.status_code == 429:
                # Rate limited - increase failure count
                _endpoint_failures[name] = _endpoint_failures.get(name, 0) + 3
            else:
                _endpoint_failures[name] = _endpoint_failures.get(name, 0) + 1

        except httpx.TimeoutException:
            _endpoint_failures[name] = _endpoint_failures.get(name, 0) + 2
            last_error = f"Timeout on {name}"
        except Exception as e:
            _endpoint_failures[name] = _endpoint_failures.get(name, 0) + 1
            last_error = f"{name}: {type(e).__name__}"

    # All endpoints failed
    return None


async def check_ip(ip_address: str) -> None | str:
    """Cached country lookup (kept for callers that only need the country)."""
    from utils.ip_facts import resolve_country

    return await resolve_country(ip_address)


async def is_valid_ip(ip: str) -> bool:
    """
    Check if a string is a valid, public IP address.

    Args:
        ip (str): The string to check.

    Returns:
        bool: True if the string is a valid public IP address, False otherwise.
    """
    return is_public_ip(ip)


IP_V6_REGEX = re.compile(r"\[([0-9a-fA-F:]+)\]:\d+\s+accepted")
IP_V4_REGEX = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
EMAIL_REGEX = re.compile(r"email:\s*([A-Za-z0-9._%+-]+)")
INBOUND_REGEX = re.compile(r"\[([^\]]+)\s+(?:>>|->)\s+[^\]]+\]")
# Regex for X-Forwarded-For header in logs (Cloudflare and other CDNs)
# Format examples:
#   xForwardedFor: 1.2.3.4
#   X-Forwarded-For: 1.2.3.4
#   xff: 1.2.3.4
XFF_REGEX = re.compile(r"(?:xForwardedFor|X-Forwarded-For|xff):\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
# Alternative regex for when Xray logs real IP in different format
# Some Xray configs show: from 1.2.3.4 (via CDN)
XRAY_REAL_IP_REGEX = re.compile(r"from\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+\(via")

# Global storage for current node information (will be set by the calling function)
CURRENT_NODE_INFO = {"node_id": None, "node_name": None}

# Global CDN configuration (loaded from config)
CDN_CONFIG = {
    "cdn_inbounds": [],  # List of inbound names that use CDN
    "cdn_provider": "cloudflare",  # CDN provider type
    "use_xff": True,  # Whether to extract real IP from X-Forwarded-For
}


async def set_current_node_info(node_id: int, node_name: str) -> None:
    """
    Set the current node information for log parsing.

    Args:
        node_id (int): The ID of the current node
        node_name (str): The name of the current node
    """
    global CURRENT_NODE_INFO
    CURRENT_NODE_INFO = {"node_id": node_id, "node_name": node_name}


_PARSER_CONFIG_CACHE: dict | None = None
_PARSER_CONFIG_CACHE_TIME: float = 0.0
_PARSER_CONFIG_REVISION: int = -1
_PARSER_OBSERVATION_CONFIG: tuple[dict, int, int] | None = None


async def get_cached_parser_config() -> dict:
    """Get configuration cached in memory with a 30-second TTL to avoid per-log I/O."""
    global _PARSER_CONFIG_CACHE, _PARSER_CONFIG_CACHE_TIME, _PARSER_CONFIG_REVISION
    current_time = time.time()
    if (
        _PARSER_CONFIG_CACHE is None
        or _PARSER_CONFIG_REVISION != get_config_revision()
        or current_time - _PARSER_CONFIG_CACHE_TIME > 30
    ):
        try:
            _PARSER_CONFIG_CACHE = await read_config()
            _PARSER_CONFIG_CACHE_TIME = current_time
            _PARSER_CONFIG_REVISION = get_config_revision()
        except Exception:
            if _PARSER_CONFIG_CACHE is None:
                _PARSER_CONFIG_CACHE = {}
    return _PARSER_CONFIG_CACHE


async def update_cdn_config() -> None:
    """
    Update the CDN configuration from the config file.
    Should be called periodically to refresh CDN settings.
    """
    global CDN_CONFIG, _PARSER_CONFIG_CACHE, _PARSER_CONFIG_CACHE_TIME
    try:
        data = await read_config()
        _PARSER_CONFIG_CACHE = data
        _PARSER_CONFIG_CACHE_TIME = time.time()
        CDN_CONFIG["cdn_inbounds"] = data.get("cdn_inbounds", [])
        CDN_CONFIG["cdn_provider"] = data.get("cdn_provider", "cloudflare")
        CDN_CONFIG["use_xff"] = data.get("cdn_use_xff", True)
    except Exception:
        pass  # Keep existing config on error


async def update_user_device_info_with_node(
    user: UserType, ip: str, inbound_protocol: str, node_id: int, node_name: str, observed_at: float | None = None
) -> None:
    """
    Update user's device information with new connection data using specific node info.

    Args:
        user (UserType): The user to update
        ip (str): The IP address
        inbound_protocol (str): The inbound protocol used
        node_id (int): The ID of the node
        node_name (str): The name of the node
    """
    current_time = time.time() if observed_at is None else observed_at

    # Check if this connection already exists
    existing_connection = None
    for conn in user.device_info.connections:
        if conn.ip == ip and conn.node_id == node_id and conn.inbound_protocol == inbound_protocol:
            existing_connection = conn
            break

    if existing_connection:
        # Update existing connection
        existing_connection.last_seen = max(existing_connection.last_seen, current_time)
        existing_connection.connection_count += 1
    else:
        # Create new connection
        new_connection = ConnectionInfo(
            ip=ip, node_id=node_id, node_name=node_name, inbound_protocol=inbound_protocol, last_seen=current_time
        )
        user.device_info.connections.append(new_connection)

    # Update sets for device analysis
    user.device_info.unique_ips.add(ip)
    user.device_info.unique_nodes.add(node_id)
    user.device_info.inbound_protocols.add(inbound_protocol)

    # Determine if user is using multiple devices
    # Consider it multi-device if:
    # 1. More than 2 unique IPs, or
    # 2. Using different inbound protocols simultaneously, or
    # 3. Active connections from different nodes at the same time
    user.device_info.is_multi_device = (
        len(user.device_info.unique_ips) > 2
        or len(user.device_info.inbound_protocols) > 1
        or len(user.device_info.unique_nodes) > 1
    )


async def update_user_device_info(user: UserType, ip: str, inbound_protocol: str) -> None:
    """
    Update user's device information with new connection data.

    Args:
        user (UserType): The user to update
        ip (str): The IP address
        inbound_protocol (str): The inbound protocol used
    """
    # Use global node info (backward compatibility)
    node_id = CURRENT_NODE_INFO["node_id"]
    node_name = CURRENT_NODE_INFO["node_name"]

    await update_user_device_info_with_node(user, ip, inbound_protocol, node_id, node_name)


async def parse_logs(
    log: str,
    node_id: int = None,
    node_name: str = None,
    config_data: dict | None = None,
    *,
    received_wall: float | None = None,
    received_monotonic: float | None = None,
) -> dict[str, UserType] | dict:  # pylint: disable=too-many-branches
    """
    Asynchronously parse logs to extract and validate IP addresses, emails, and inbound protocols.

    Args:
        log (str): The log to parse.
        node_id (int): The ID of the node that generated this log
        node_name (str): The name of the node that generated this log
        config_data (dict, optional): Pre-read configuration data. If None, uses fast in-memory cache.

    Returns:
        dict[str, UserType]: Dictionary of users with their connection information
    """
    global _PARSER_OBSERVATION_CONFIG

    received_wall = time.time() if received_wall is None else received_wall
    received_monotonic = time.monotonic() if received_monotonic is None else received_monotonic
    admission = monitoring_snapshot()
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return ACTIVE_USERS
    # Use provided node info or fall back to global (for backward compatibility)
    current_node_id = node_id if node_id is not None else CURRENT_NODE_INFO.get("node_id")
    current_node_name = node_name if node_name is not None else CURRENT_NODE_INFO.get("node_name")

    data = config_data if config_data is not None else await get_cached_parser_config()
    revision = get_config_revision()
    if data.get("_revision", revision) != revision or data.get("ip_source", "logs") != "logs":
        return ACTIVE_USERS
    async with ACTIVE_USERS_LOCK:
        if revision != get_config_revision() or not monitoring_is_current(admission):
            return ACTIVE_USERS
        configured = _PARSER_OBSERVATION_CONFIG
        authoritative_cached_config = (
            config_data is None and data is _PARSER_CONFIG_CACHE and _PARSER_CONFIG_REVISION == revision
        )
        if (
            not authoritative_cached_config
            or configured is None
            or configured[0] is not data
            or configured[1] != revision
            or configured[2] != LOG_OBSERVATIONS.generation
        ):
            if LOG_OBSERVATIONS.configure(data):
                ACTIVE_USERS.clear()
            _PARSER_OBSERVATION_CONFIG = (
                (data, revision, LOG_OBSERVATIONS.generation) if authoritative_cached_config else None
            )
        # An excluded stream may still finish a pending read. Reject its records
        # before timezone conversion or any preparation can affect Logs evidence.
        if not LOG_OBSERVATIONS.monitors(current_node_id):
            return ACTIVE_USERS
        generation = LOG_OBSERVATIONS.generation
        node_epoch = LOG_OBSERVATIONS.node_epoch(current_node_id)
    if data.get("INVALID_IPS"):
        INVALID_IPS.update(data.get("INVALID_IPS"))

    # Update CDN config from settings
    cdn_inbounds = data.get("cdn_inbounds", [])
    use_xff = data.get("cdn_use_xff", True)

    prepared_records = []
    unknown_ids = set()
    for line in log.splitlines():
        if "accepted" not in line or "BLOCK]" in line:
            continue
        email_match = EMAIL_REGEX.search(line)
        if email_match is None:
            continue
        raw_email = email_match.group(1)
        if raw_email.isdigit() and raw_email not in LOCAL_ID_CACHE:
            unknown_ids.add(raw_email)
        ip_match = IP_V6_REGEX.search(line) or IP_V4_REGEX.search(line)
        if ip_match is None:
            continue
        inbound_match = INBOUND_REGEX.search(line)
        inbound_protocol = inbound_match.group(1).strip() if inbound_match else "Unknown"
        observed_at = None
        timestamp_fault = ""
        future_fault = None
        try:
            observed_at = LOG_OBSERVATIONS.timestamp(line, current_node_id)
        except FutureTimestampError as error:
            timestamp_fault = "future_timestamp"
            future_fault = error
        except ValueError as error:
            timestamp_fault = LOG_OBSERVATIONS.timestamp_failure_reason(error)
        prepared_records.append(
            (line, ip_match.group(1), raw_email, inbound_protocol, observed_at, timestamp_fault, future_fault)
        )

    # Pre-resolve unknown numeric IDs once, using the matches already captured.
    if unknown_ids:
        from db.database import get_db
        from db.crud.users import UserCRUD

        try:
            async with get_db() as db:
                if not monitoring_is_current(admission):
                    return ACTIVE_USERS
                for numeric_id in unknown_ids:
                    real_username = await UserCRUD.get_username_by_panel_id(db, int(numeric_id))
                    if not monitoring_is_current(admission):
                        return ACTIVE_USERS
                    if real_username:
                        LOCAL_ID_CACHE[numeric_id] = real_username
        except Exception as e:
            parse_logger.error(f"Error batch-resolving numeric IDs: {e}")

    for line, ip, raw_email, inbound_protocol, observed_at, timestamp_fault, future_fault in prepared_records:
        if not monitoring_is_current(admission):
            return ACTIVE_USERS

        # Check if this inbound is a CDN inbound - if so, try to get real IP from X-Forwarded-For
        is_cdn_inbound = inbound_protocol in cdn_inbounds
        if is_cdn_inbound and use_xff:
            # Try to extract real IP from X-Forwarded-For header
            xff_match = XFF_REGEX.search(line)
            if xff_match:
                real_ip = xff_match.group(1)
                # Use the real IP from X-Forwarded-For instead of CDN edge IP
                ip = real_ip
            else:
                # Try alternative Xray real IP format
                real_ip_match = XRAY_REAL_IP_REGEX.search(line)
                if real_ip_match:
                    ip = real_ip_match.group(1)

        # Admission decision (validity, node blocklist, country) - shared with
        # API mode so both sources accept exactly the same addresses.
        if not await is_ip_accepted(ip, data):
            continue

        # Extract email
        # Decide panel ID vs username before stripping a numeric prefix, so a
        # prefixed numeric username cannot be credited to a different panel ID.
        if raw_email.isdigit():
            email = LOCAL_ID_CACHE.get(raw_email)
            if not email:
                continue
        else:
            email = await remove_id_from_username(raw_email)
        if not email or not email.strip() or email in INVALID_EMAILS:
            continue
        # Preparation may have awaited country or numeric-user resolution. Commit
        # the fingerprint only now, under the same lock as the observation.
        async with ACTIVE_USERS_LOCK:
            if (
                generation != LOG_OBSERVATIONS.generation
                or node_epoch != LOG_OBSERVATIONS.node_epoch(current_node_id)
                or revision != get_config_revision()
                or not LOG_OBSERVATIONS.monitors(current_node_id)
                or not monitoring_is_current(admission)
            ):
                continue
            if observed_at is None:
                until = future_fault.source_epoch + LOG_OBSERVATIONS.freshness_seconds if future_fault else None
                LOG_OBSERVATIONS.mark_uncertain(
                    until, node_id=current_node_id, reason=timestamp_fault or "invalid_timestamp"
                )
                if future_fault:
                    LOG_OBSERVATIONS.record_timestamp_fault(
                        current_node_id,
                        future_fault.source_epoch,
                        phase="timestamp_parse",
                        evaluation_wall=future_fault.evaluation_wall,
                        evaluation_monotonic=future_fault.evaluation_monotonic,
                        line=line,
                        received_wall=received_wall,
                        received_monotonic=received_monotonic,
                    )
                continue
            if not LOG_OBSERVATIONS.admit(
                current_node_id,
                line,
                observed_at,
                received_wall=received_wall,
                received_monotonic=received_monotonic,
            ):
                continue
            user = ACTIVE_USERS.get(email)
            if user:
                # Add IP if not already present
                if ip not in user.ip:
                    user.ip.append(ip)
                # Update device info with the specific node info
                await update_user_device_info_with_node(
                    user, ip, inbound_protocol, current_node_id, current_node_name, observed_at
                )
            else:
                # Create new user
                user = UserType(name=email, ip=[ip])
                await update_user_device_info_with_node(
                    user, ip, inbound_protocol, current_node_id, current_node_name, observed_at
                )
                ACTIVE_USERS[email] = user

    return ACTIVE_USERS


async def clear_node_active_connections(node_id: int) -> int:
    """
    Atomically clear active connections belonging to a specific node_id from ACTIVE_USERS.
    This prevents ghost connections when an SSE connection drops or reconnects.

    Args:
        node_id (int): The ID of the node to clear connections for.

    Returns:
        int: Number of connections removed.
    """
    if node_id is None:
        return 0

    removed_count = 0
    users_to_cleanup = []

    async with ACTIVE_USERS_LOCK:
        for username, user in list(ACTIVE_USERS.items()):
            if not hasattr(user, "device_info") or not user.device_info or not user.device_info.connections:
                continue

            # Build new connection list atomically
            new_connections = [conn for conn in user.device_info.connections if conn.node_id != node_id]

            removed_from_user = len(user.device_info.connections) - len(new_connections)
            if removed_from_user > 0:
                removed_count += removed_from_user

                if new_connections:
                    # Recompute sets atomically
                    new_ips = {conn.ip for conn in new_connections}
                    new_nodes = {conn.node_id for conn in new_connections if conn.node_id is not None}
                    new_protocols = {
                        conn.inbound_protocol for conn in new_connections if conn.inbound_protocol is not None
                    }

                    # Atomic assignment
                    user.device_info.connections = new_connections
                    user.device_info.unique_ips = new_ips
                    user.device_info.unique_nodes = new_nodes
                    user.device_info.inbound_protocols = new_protocols
                    user.device_info.is_multi_device = len(new_ips) > 2 or len(new_protocols) > 1 or len(new_nodes) > 1
                    user.ip = list(new_ips)
                else:
                    users_to_cleanup.append(username)

        for username in users_to_cleanup:
            if username in ACTIVE_USERS:
                del ACTIVE_USERS[username]

    return removed_count
