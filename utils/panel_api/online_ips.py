"""
Panel API transport for online-IP statistics.

PasarGuard exposes the connected IPs of a user through its node online-stats
endpoints. There is no bulk variant, so callers perform a bounded-concurrency
fan-out over a narrowed candidate set:

    GET /api/node/online_stats/{user_id}/ip
        -> {"nodes": {<node_id>: {"ips": {"<ip>": <last_seen_epoch>}} | null}}

    GET /api/users?group=..&status=..&online=true
        -> {"users": [...], "total": N}

Both endpoints require the configured panel account to hold the
``nodes:stats`` permission.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.logs import get_logger
from utils.types import PanelType

online_ips_logger = get_logger("panel_api.online_ips")

# Outcome codes returned alongside the payload by fetch_user_online_ips()
OUTCOME_OK = "ok"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_FORBIDDEN = "forbidden"
OUTCOME_UNAUTHORIZED = "unauthorized"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_DEFERRED = "deferred"


class OnlineIPTimeout(str):
    """Keep the timeout outcome compatible while retaining its transport phase."""

    def __new__(cls, timeout_kind: str | None):
        instance = super().__new__(cls, OUTCOME_TIMEOUT)
        instance.timeout_kind = timeout_kind
        return instance


# Panel-side fixed window used by the `online=true` filter (app/db/crud/user.py)
PANEL_ONLINE_WINDOW_SECONDS = 120

# Values below this cannot be contemporary Xray timestamps. Their age is unknown.
STALE_EPOCH_FLOOR = 1_000_000_000


def build_online_after(window_seconds: int) -> str:
    """Build an ISO-8601 UTC timestamp for ``now - window_seconds``."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(window_seconds)))
    return moment.replace(microsecond=0).isoformat()


async def fetch_online_candidates(
    panel_data: PanelType,
    group_ids: Optional[list[int]] = None,
    admin_usernames: Optional[list[str]] = None,
    status: Optional[str] = "active",
    online_window: int = 0,
    page_size: int = 500,
    max_concurrent: int = 1,
) -> Optional[list[dict]]:
    """
    Fetch the users that are candidates for an online-IP lookup.

    Narrowing happens server-side so that only a small slice of the user base
    is ever paged over. ``group_ids`` maps to the panel's ``group`` query
    parameter (ANY-of semantics) and is the main lever: with monitoring
    restricted to a handful of limited-device groups, the candidate set drops
    from the whole panel to just the online members of those groups.

    Args:
        panel_data: Panel connection configuration.
        group_ids: Restrict to users belonging to any of these group IDs.
        admin_usernames: Restrict to users owned by these admins.
        status: Panel status filter, ``None`` to disable.
        online_window: Retained for callers that probe all users with ``0``.
            Any positive value selects the panel's ``online=true`` filter;
            the limiter always selects this filter independently of its interval.
        page_size: Users per page.
        max_concurrent: Max concurrent page requests.

    Returns:
        Raw user dictionaries, each including at least ``id`` and ``username``,
        or ``None`` if the query itself failed. The distinction matters: an
        empty list means nobody is online, while ``None`` means the candidate
        set is unknown and the caller must not treat the panel as idle.
        A changing live listing carries ``pagination_changed=True``: its
        observed users can be checked, but omitted users are not proven offline.
    """
    from utils.panel_api.users import fetch_all_users_raw

    try:
        return await fetch_all_users_raw(
            panel_data,
            status=status,
            admin=admin_usernames or None,
            group=group_ids or None,
            limit=page_size,
            max_concurrent=max_concurrent,
            online=online_window > 0,
            online_after=None,
            sort="username",
            strict=True,
            allow_live_changes=True,
            quiet_requests=True,
        )
    except Exception as error:
        online_ips_logger.error(f"Failed to fetch online candidates: {error}")
        return None


async def fetch_user_memberships(
    panel_data: PanelType,
    user_ids: list[int],
    group_ids: Optional[list[int]] = None,
    admin_usernames: Optional[list[str]] = None,
) -> Optional[list[dict]]:
    """Confirm scanned users' current access independently of online presence."""
    from utils.panel_api.users import PanelUserListing, fetch_all_users_raw

    if any(type(uid) is not int or uid < 1 for uid in user_ids):
        return None
    identities = sorted(set(user_ids))
    result = PanelUserListing([])
    try:
        for offset in range(0, len(identities), 200):
            rows = await fetch_all_users_raw(
                panel_data,
                ids=identities[offset : offset + 200],
                status="active",
                admin=admin_usernames or None,
                group=group_ids or None,
                limit=200,
                max_concurrent=1,
                sort="username",
                strict=True,
                allow_live_changes=True,
                quiet_requests=True,
            )
            result.extend(rows)
            result.pagination_changed |= rows.pagination_changed
    except Exception as error:
        online_ips_logger.error("Failed to confirm user memberships: %s", type(error).__name__)
        return None
    return result


def _parse_ip_payload(payload: dict) -> dict[int, dict[str, int] | None]:
    """Preserve timestamps, empty nodes and unavailable nodes without coercion.

    A malformed body is unknown evidence. In particular, booleans and legacy
    connection counts must never be promoted to a current observation.
    """
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, dict):
        raise ValueError("Online-IP response has no node map")
    parsed = {}
    for node_key, node_value in nodes.items():
        if type(node_key) is not int and not (isinstance(node_key, str) and node_key.removeprefix("-").isdecimal()):
            raise ValueError("Invalid node ID")
        try:
            node_id = int(node_key)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid node ID") from error
        if node_id in parsed:
            raise ValueError("Duplicate normalized node ID")
        if node_value is None:
            parsed[node_id] = None
            continue
        if not isinstance(node_value, dict) or not isinstance(node_value.get("ips"), dict):
            raise ValueError("Invalid node IP map")
        ip_values = {}
        for ip, value in node_value["ips"].items():
            if not isinstance(ip, str) or not ip.strip() or type(value) is not int:
                raise ValueError("Invalid IP timestamp")
            normalized_ip = ip.strip()
            if normalized_ip in ip_values:
                raise ValueError("Duplicate normalized IP")
            ip_values[normalized_ip] = value
        parsed[node_id] = ip_values
    return parsed


async def fetch_user_online_ips(
    panel_data: PanelType,
    user_id: int,
    timeout: float = 20.0,
) -> tuple[Optional[dict[int, dict[str, int] | None]], str]:
    """
    Fetch the online IPs of one user across every healthy node.

    A single attempt is made on purpose: this runs inside a fan-out of
    potentially thousands of requests per cycle, where aggressive retries
    would amplify a panel hiccup into an outage. Transient failures are
    absorbed by the caller's coverage accounting instead.

    Returns:
        ``(payload, outcome)`` where ``payload`` is
        ``{node_id: {ip: last_seen_epoch} | None}`` on success and ``None``
        otherwise. A null node is unavailable evidence, not an empty map, and ``outcome`` is one of the ``OUTCOME_*`` constants.
    """
    from utils.panel_api.request_helper import online_stats_circuit_breaker, panel_request

    response, error = await panel_request(
        panel_data,
        "GET",
        f"/api/node/online_stats/{user_id}/ip",
        timeout=timeout,
        max_retries=1,
        breaker=online_stats_circuit_breaker,
        quiet=True,
    )

    if response is not None:
        if response.status_code == 200:
            try:
                return _parse_ip_payload(response.json()), OUTCOME_OK
            except Exception as parse_error:
                online_ips_logger.debug(f"Unparseable online-IP body for user {user_id}: {type(parse_error).__name__}")
                return None, OUTCOME_ERROR
        if response.status_code == 404:
            # Only the panel's explicit missing-user response proves absence.
            # A missing route or proxy 404 leaves connection evidence unknown.
            try:
                body = response.json()
            except (ValueError, AttributeError):
                return None, OUTCOME_ERROR
            if isinstance(body, dict) and body.get("detail") == "User not found":
                return None, OUTCOME_NOT_FOUND
            return None, OUTCOME_ERROR
        if response.status_code == 401:
            return None, OUTCOME_UNAUTHORIZED
        if response.status_code == 403:
            return None, OUTCOME_FORBIDDEN
        return None, OUTCOME_ERROR

    if getattr(error, "reason", None) == "deferred":
        return None, OUTCOME_DEFERRED
    if getattr(error, "reason", None) == "timeout":
        return None, OnlineIPTimeout(getattr(error, "timeout_kind", None))
    return None, OUTCOME_ERROR


async def check_online_stats_permission(panel_data: PanelType, user_id: int, timeout: float = 10.0) -> tuple[bool, str]:
    """
    Pre-flight the online-stats endpoint against a single user.

    Used before switching the limiter into API mode: without the
    ``nodes:stats`` permission every request would fail, which would silently
    disable enforcement altogether.

    Returns:
        ``(ok, human_readable_reason)``.
    """
    _, outcome = await fetch_user_online_ips(panel_data, user_id, timeout=timeout)
    if outcome in (OUTCOME_OK, OUTCOME_NOT_FOUND):
        return True, "Online-stats endpoint reachable."
    if outcome == OUTCOME_FORBIDDEN:
        return False, (
            "Panel API key lacks the <code>nodes:stats</code> permission required by /api/node/online_stats."
        )
    if outcome == OUTCOME_UNAUTHORIZED:
        return False, "Panel rejected the API key (401)."
    return False, "Online-stats endpoint did not respond successfully."
