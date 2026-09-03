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

# Panel-side fixed window used by the `online=true` filter (app/db/crud/user.py)
PANEL_ONLINE_WINDOW_SECONDS = 120

# Per-IP values at or above this are Unix timestamps (last seen), below it they
# are treated as legacy connection counts and are never filtered for staleness.
STALE_EPOCH_FLOOR = 1_000_000_000


async def resolve_panel_token(
    panel_data: PanelType, force_refresh: bool = False
) -> Optional[str]:
    """
    Resolve a bearer token once per collection cycle.

    The fan-out reuses a single token instead of asking the auth layer per
    request, which would turn one cycle into thousands of cache lookups.
    """
    from utils.panel_api.auth import get_token

    try:
        token_result = await get_token(panel_data, force_refresh=force_refresh)
    except Exception as error:
        online_ips_logger.error(f"Token acquisition failed: {error}")
        return None
    if isinstance(token_result, ValueError):
        online_ips_logger.error(f"Token acquisition failed: {token_result}")
        return None
    return token_result.panel_token


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
    max_concurrent: int = 5,
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
        online_window: Freshness window in seconds. ``0`` disables the online
            filter entirely (every matching user is returned). Windows within
            the panel's own 2-minute window use the cheap ``online=true``
            flag; wider windows use an explicit ``online_after`` timestamp so
            that freshness tracks the configured check interval.
        page_size: Users per page.
        max_concurrent: Max concurrent page requests.

    Returns:
        Raw user dictionaries, each including at least ``id`` and ``username``,
        or ``None`` if the query itself failed. The distinction matters: an
        empty list means nobody is online, while ``None`` means the candidate
        set is unknown and the caller must not treat the panel as idle.
    """
    from utils.panel_api.users import fetch_all_users_raw

    use_online_flag = False
    online_after = None
    if online_window > 0:
        if online_window <= PANEL_ONLINE_WINDOW_SECONDS - 10:
            use_online_flag = True
        else:
            online_after = build_online_after(online_window)

    try:
        return await fetch_all_users_raw(
            panel_data,
            status=status,
            admin=admin_usernames or None,
            group=group_ids or None,
            limit=page_size,
            max_concurrent=max_concurrent,
            online=use_online_flag,
            online_after=online_after,
        )
    except Exception as error:
        online_ips_logger.error(f"Failed to fetch online candidates: {error}")
        return None


def _parse_ip_payload(payload: dict) -> dict[int, dict[str, int]]:
    """
    Normalise a ``UserIPListAll`` body into ``{node_id: {ip: value}}``.

    The per-IP value is the panel's **last-seen Unix timestamp** for that IP on
    that node (verified against a live panel: e.g. ``1788078039`` = 2026-08-30
    08:20 UTC). Older panel builds documented it as a connection count, and a value
    this function cannot parse is coerced to ``1``, so callers must treat anything
    below ``STALE_EPOCH_FLOOR`` as an unknown age rather than as a fresh connection -
    the panel never expires an entry, so counting one would resurrect the
    changed-network-looks-like-several-devices failure.

    Nodes the panel could not reach come back as ``null`` and are skipped, as
    are nodes that reported no IPs at all.
    """
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, dict):
        return {}

    parsed: dict[int, dict[str, int]] = {}
    for node_key, node_value in nodes.items():
        if not isinstance(node_value, dict):
            continue
        raw_ips = node_value.get("ips")
        if not isinstance(raw_ips, dict) or not raw_ips:
            continue
        try:
            node_id = int(node_key)
        except (ValueError, TypeError):
            continue

        ip_values: dict[str, int] = {}
        for ip, value in raw_ips.items():
            ip_str = str(ip).strip()
            if not ip_str:
                continue
            try:
                ip_values[ip_str] = max(1, int(value))
            except (ValueError, TypeError):
                ip_values[ip_str] = 1
        if ip_values:
            parsed[node_id] = ip_values
    return parsed


async def fetch_user_online_ips(
    panel_data: PanelType,
    user_id: int,
    token: str,
    timeout: float = 8.0,
) -> tuple[Optional[dict[int, dict[str, int]]], str]:
    """
    Fetch the online IPs of one user across every healthy node.

    A single attempt is made on purpose: this runs inside a fan-out of
    potentially thousands of requests per cycle, where aggressive retries
    would amplify a panel hiccup into an outage. Transient failures are
    absorbed by the caller's coverage accounting instead.

    Returns:
        ``(payload, outcome)`` where ``payload`` is
        ``{node_id: {ip: last_seen_epoch}}`` on success and ``None``
        otherwise, and ``outcome`` is one of the ``OUTCOME_*`` constants.
    """
    from utils.panel_api.request_helper import panel_request

    response, error = await panel_request(
        panel_data,
        "GET",
        f"/api/node/online_stats/{user_id}/ip",
        token,
        timeout=timeout,
        max_retries=1,
    )

    if response is not None:
        if response.status_code == 200:
            try:
                return _parse_ip_payload(response.json()), OUTCOME_OK
            except Exception as parse_error:
                online_ips_logger.debug(
                    f"Unparseable online-IP body for user {user_id}: {parse_error}"
                )
                return None, OUTCOME_ERROR
        if response.status_code == 404:
            # User was deleted from the panel between the candidate query and now
            return None, OUTCOME_NOT_FOUND
        if response.status_code == 401:
            return None, OUTCOME_UNAUTHORIZED
        if response.status_code == 403:
            return None, OUTCOME_FORBIDDEN
        return None, OUTCOME_ERROR

    # panel_request() collapses non-401/404 error responses into an error
    # string, so 403 has to be recovered from there.
    if error and "HTTP 403" in error:
        return None, OUTCOME_FORBIDDEN
    return None, OUTCOME_ERROR


async def check_online_stats_permission(
    panel_data: PanelType, user_id: int, timeout: float = 10.0
) -> tuple[bool, str]:
    """
    Pre-flight the online-stats endpoint against a single user.

    Used before switching the limiter into API mode: without the
    ``nodes:stats`` permission every request would fail, which would silently
    disable enforcement altogether.

    Returns:
        ``(ok, human_readable_reason)``.
    """
    token = await resolve_panel_token(panel_data)
    if not token:
        return False, "Could not obtain a panel token."

    _, outcome = await fetch_user_online_ips(panel_data, user_id, token, timeout=timeout)
    if outcome in (OUTCOME_OK, OUTCOME_NOT_FOUND):
        return True, "Online-stats endpoint reachable."
    if outcome == OUTCOME_FORBIDDEN:
        return False, (
            "Panel account lacks the <code>nodes:stats</code> permission "
            "required by /api/node/online_stats."
        )
    if outcome == OUTCOME_UNAUTHORIZED:
        return False, "Panel rejected the token (401)."
    return False, "Online-stats endpoint did not respond successfully."
