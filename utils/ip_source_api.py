"""
API-based IP source.

Alternative to SSE log streaming: instead of parsing node logs, the connected
IPs are pulled from the panel's online-stats endpoints once per check cycle
and written into the exact same ``ACTIVE_USERS`` structure that
``utils.parse_logs`` produces. Everything downstream — device counting,
subnet/CDN/high-trust grouping, the consecutive-violation warning system,
trust scoring and punishment — is therefore unchanged.

Collection is a two-stage process:

1. Candidate narrowing. ``GET /api/users`` is filtered server-side by the
   monitored group IDs, the panel status and an online-freshness window, so
   only a small slice of the user base is ever considered. The same response
   supplies the numeric ``id`` needed by stage 2.
2. Bounded-concurrency fan-out. One ``GET /api/node/online_stats/{id}/ip``
   request per candidate, capped by a semaphore sized against the shared
   httpx connection pool.
"""

import asyncio
import time
from typing import Optional

from utils.logs import get_logger
from utils.read_config import normalize_min_coverage
from utils.shared_state import ACTIVE_USERS, ACTIVE_USERS_LOCK
from utils.types import PanelType, UserType

api_ip_logger = get_logger("ip_source_api")

# Stats of the most recent collection cycle, surfaced by the Telegram menu.
LAST_CYCLE_STATS: dict = {
    "ran_at": 0.0,
    "candidates": 0,
    "prefiltered": 0,
    "fetched_ok": 0,
    "fetched_failed": 0,
    "not_found": 0,
    "forbidden": 0,
    "coverage": 0.0,
    "users_with_ips": 0,
    "total_ips": 0,
    "nodes_seen": 0,
    # How many nodes the panel says should be reporting, and the resulting ratio.
    # `coverage` above is the per-USER answer rate and says nothing about nodes: a
    # cycle where every user call returned 200 reads 1.0 even if two thirds of the
    # nodes contributed no IPs at all, which under-counts devices silently.
    "nodes_expected": 0,
    "node_coverage": 0.0,
    "duration_ms": 0,
    "geo_lookups": 0,
    "stale_ips": 0,
    # Declared here so _reset_cycle_stats zeroes them: it iterates the live dict, and a
    # key that only ever arrives via update(build_stats) would keep the previous cycle's
    # value on any cycle that returns before the build.
    "future_ips": 0,
    "unknown_age_ips": 0,
    "skipped_reason": "",
}

# Consecutive cycles that produced no usable data at all. Used to fall back to
# log mode when the panel account cannot serve the online-stats endpoint.
_consecutive_dead_cycles = 0
_forbidden_alert_sent = False

# Consecutive cycles skipped because the sample was too incomplete to act on. Kept
# apart from the dead-cycle streak: a dead cycle means the panel gave us nothing and
# log mode is the remedy, while these mean the panel answered incompletely and the
# remedy is for the operator to look at why.
_consecutive_coverage_skips = 0
_coverage_alert_sent = False

# Consecutive dead cycles tolerated before reverting to log mode
DEAD_CYCLE_FALLBACK_THRESHOLD = 3

# Consecutive coverage skips tolerated before saying enforcement has stopped
COVERAGE_SKIP_ALERT_THRESHOLD = 3


def get_last_cycle_stats() -> dict:
    """Return a copy of the most recent collection cycle statistics."""
    return dict(LAST_CYCLE_STATS)


async def _notify(message: str) -> None:
    """Best-effort Telegram notification; never raises into the collector."""
    try:
        from telegram_bot.send_message import send_logs

        await send_logs(message)
    except Exception as error:
        api_ip_logger.debug(f"Could not deliver notification: {error}")


async def _resolve_node_context(panel_data: PanelType, config_data: dict) -> tuple[dict, set, set]:
    """
    Build the node lookup table and refresh the node-IP blocklist.

    In log mode ``create_node_task`` adds every node IP to the shared blocklist
    so that inter-node relay traffic is never counted as a user device. API mode
    does not go through that path, so the same seeding happens here.

    Returns:
        ``(node_name_map, disabled_node_ids, expected_node_ids)``

    ``expected_node_ids`` are the connected nodes the operator has not disabled -
    the set that ought to show up in a healthy cycle. Without it there is no way
    to tell "users are concentrated on 16 nodes" from "33 nodes stopped
    reporting", and the two have opposite meanings for the device counts.
    """
    from utils.ip_facts import register_node_ips
    from utils.panel_api.nodes import get_nodes

    node_name_map: dict[int, str] = {}
    connected_node_ids: set = set()
    try:
        nodes = await get_nodes(panel_data)
    except Exception as error:
        api_ip_logger.warning(f"Could not resolve node list: {error}")
        nodes = None

    if nodes and not isinstance(nodes, ValueError):
        register_node_ips(node.node_ip for node in nodes)
        for node in nodes:
            node_name_map[node.node_id] = node.node_name
            if getattr(node, "status", None) == "connected":
                connected_node_ids.add(node.node_id)

    disabled_nodes = set(config_data.get("disabled_nodes") or [])
    return node_name_map, disabled_nodes, connected_node_ids - disabled_nodes


def resolve_monitored_group_ids(config_data: dict) -> Optional[list[int]]:
    """
    Determine which group IDs the candidate query may be restricted to.

    Narrowing is only allowed when it provably cannot drop a user enforcement would
    judge, and exactly one setting gives that guarantee: the Group Filter in
    ``include`` mode. There, ``calculate_user_effective_limit_and_monitoring``
    (``utils/user_sync.py``) sets ``is_monitored=False`` for every user outside the
    listed groups, and the enforcement gate in ``check_usage`` skips on that flag, so
    the users the panel omits are the same ones enforcement would ignore.

    Everything else returns ``None`` - no group narrowing at all. The candidate query
    is still bounded by ``status=active`` and the online-freshness window, which is
    what keeps it cheap.

    Group Limits deliberately do **not** narrow. That mapping only supplies an
    *effective limit* per group; a user in no limited group is still monitored and is
    judged against the general limit (``resolve_effective_limit`` step 5). Restricting
    the query to the ``group_limits`` keys therefore left every general-limit user
    uncollected, and because coverage is measured over the narrowed target set the
    cycle still reported 100% and enforced - silently shrinking enforcement to the
    limited groups while looking healthy.

    Note the degenerate case: filter enabled in ``include`` mode with an empty group
    list makes *nobody* monitored, but an empty return value means "no filter" to the
    panel, so this returns ``None`` and lets ``_prefilter_candidates`` drop them all.
    One wasted query, correct outcome.
    """
    group_filter = config_data.get("group_filter") or {}
    if group_filter.get("enabled") and group_filter.get("mode", "include") == "include":
        filter_ids = [int(g) for g in (group_filter.get("group_ids") or [])]
        if filter_ids:
            return filter_ids

    return None


def resolve_monitored_admins(_config_data: dict) -> Optional[list[str]]:
    """
    Always ``None``: the admin filter cannot be expressed as a panel-side filter.

    The configuration is still taken so the call site reads like its group counterpart
    and so a future panel that can express the null-owner case has somewhere to read
    from.

    The enforcement gate in ``check_usage`` is deliberately fail-open on ownership - a
    user whose local ``owner_username`` is NULL is still limited, because a sync gap
    must not silently exempt anyone. The panel's ``admin`` parameter is an ANY-of match
    over named admins, so it cannot express "…or has no known owner" and would drop
    exactly those users from the query while enforcement still judged them.

    Same rule as ``resolve_monitored_group_ids``: narrowing is only allowed where it
    provably cannot drop a user enforcement would judge. It does not hold here, so the
    admin filter stays client-side. The query is still bounded by ``status=active`` and
    the online-freshness window.
    """
    return None


def _prefilter_candidates(candidates: list[dict]) -> list[tuple[str, int, dict]]:
    """
    Drop candidates that enforcement would ignore anyway.

    Users already known to be excepted or unmonitored are removed before the
    fan-out, which is the cheapest possible reduction in panel load. Users
    absent from the metadata cache are kept (fail-open), matching how log mode
    treats not-yet-synced users.

    Returns:
        ``[(username, panel_user_id, raw_user_dict), ...]``
    """
    from utils.user_sync import USER_METADATA_CACHE

    selected: list[tuple[str, int, dict]] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        username = raw.get("username")
        user_id = raw.get("id")
        if not username or user_id is None:
            continue
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            continue

        metadata = USER_METADATA_CACHE.get(username)
        if metadata is not None:
            if metadata.get("is_excepted"):
                continue
            if metadata.get("is_monitored") is False:
                continue
        selected.append((username, user_id, raw))
    return selected


async def _validate_ips(raw_ips: set[str], config_data: dict) -> tuple[set[str], int]:
    """
    Apply the shared IP admission rule to the whole cycle at once.

    The decision itself lives in ``utils.ip_facts`` so log mode and API mode can
    never diverge on whether an address counts: private/malformed addresses and
    node addresses are rejected, and when a country is configured the address
    must resolve to it. A failed geo lookup is accepted and left uncached.

    Returns:
        ``(accepted_ips, geo_lookup_count)``
    """
    from utils.ip_facts import geo_lookup_count, is_ip_accepted

    if not raw_ips:
        return set(), 0

    lookups_before = geo_lookup_count()
    semaphore = asyncio.Semaphore(5)

    async def _decide(ip: str) -> tuple[str, bool]:
        async with semaphore:
            try:
                return ip, await is_ip_accepted(ip, config_data)
            except Exception:
                return ip, False

    accepted: set[str] = set()
    results = await asyncio.gather(
        *[_decide(ip) for ip in raw_ips], return_exceptions=True
    )
    for item in results:
        if isinstance(item, tuple) and item[1]:
            accepted.add(item[0])

    return accepted, geo_lookup_count() - lookups_before


async def _fetch_all_online_ips(
    panel_data: PanelType,
    targets: list[tuple[str, int, dict]],
    concurrency: int,
    timeout: float,
) -> tuple[dict[str, dict[int, dict[str, int]]], dict[str, int], dict[str, float]]:
    """
    Run the bounded-concurrency fan-out over the candidate users.

    Returns:
        ``({username: {node_id: {ip: last_seen}}}, counters, {username: fetched_at})``

    ``fetched_at`` matters because the fan-out can take minutes on a slow
    panel: freshness has to be judged against the moment that user was
    sampled, not against the moment the whole pass finished.
    """
    from utils.panel_api.online_ips import (
        OUTCOME_FORBIDDEN,
        OUTCOME_NOT_FOUND,
        OUTCOME_OK,
        OUTCOME_UNAUTHORIZED,
        fetch_user_online_ips,
        resolve_panel_token,
    )

    counters = {"ok": 0, "failed": 0, "not_found": 0, "forbidden": 0, "unauthorized": 0}
    payloads: dict[str, dict[int, dict[str, int]]] = {}
    fetch_times: dict[str, float] = {}
    if not targets:
        return payloads, counters, fetch_times

    token = await resolve_panel_token(panel_data)
    if not token:
        counters["failed"] = len(targets)
        return payloads, counters, fetch_times

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(username: str, user_id: int, active_token: str):
        async with semaphore:
            payload, outcome = await fetch_user_online_ips(
                panel_data, user_id, active_token, timeout=timeout
            )
            return username, user_id, payload, outcome, time.time()

    async def _run(batch: list[tuple[str, int]], active_token: str) -> list[tuple[str, int]]:
        """Execute one pass; return the subset that needs a token refresh."""
        needs_retry: list[tuple[str, int]] = []
        results = await asyncio.gather(
            *[_one(name, uid, active_token) for name, uid in batch],
            return_exceptions=True,
        )
        for item in results:
            if not isinstance(item, tuple):
                counters["failed"] += 1
                continue
            username, user_id, payload, outcome, fetched_at = item
            if outcome == OUTCOME_OK:
                counters["ok"] += 1
                if payload:
                    payloads[username] = payload
                    fetch_times[username] = fetched_at
            elif outcome == OUTCOME_NOT_FOUND:
                counters["not_found"] += 1
            elif outcome == OUTCOME_FORBIDDEN:
                counters["forbidden"] += 1
            elif outcome == OUTCOME_UNAUTHORIZED:
                needs_retry.append((username, user_id))
            else:
                counters["failed"] += 1
        return needs_retry

    batch = [(name, uid) for name, uid, _ in targets]
    retry_batch = await _run(batch, token)

    # A token can expire mid-cycle; refresh once and replay only the 401s so a
    # whole cycle is not lost to an expiry.
    if retry_batch:
        api_ip_logger.info(f"🔑 Refreshing token and replaying {len(retry_batch)} unauthorized lookups")
        fresh_token = await resolve_panel_token(panel_data, force_refresh=True)
        if fresh_token:
            still_failing = await _run(retry_batch, fresh_token)
            counters["unauthorized"] += len(still_failing)
            counters["failed"] += len(still_failing)
        else:
            counters["unauthorized"] += len(retry_batch)
            counters["failed"] += len(retry_batch)

    return payloads, counters, fetch_times


def _extract_admin_username(raw: dict) -> Optional[str]:
    """Read the owning admin's username from a raw ``/api/users`` entry."""
    admin = raw.get("admin")
    if isinstance(admin, dict):
        return admin.get("username")
    if isinstance(admin, str):
        return admin
    return raw.get("admin_username")


async def _build_users_from_payloads(
    payloads: dict[str, dict[int, dict[str, int]]],
    raw_by_name: dict[str, dict],
    node_name_map: dict[int, str],
    disabled_nodes: set,
    config_data: dict,
    fetch_times: dict[str, float] | None = None,
) -> tuple[dict[str, UserType], dict]:
    """
    Convert the fan-out payloads into the ``ACTIVE_USERS`` structure.

    The panel reports ``{node_id: {ip: last_seen_epoch}}`` with no inbound
    information, so a single sentinel inbound name stands in for all of them.
    The device-counting key therefore keeps its ``(ip, inbound_protocol)`` shape
    and subnet and CDN-node grouping continue to work unchanged.

    The panel keeps an IP in that map long after the client stopped using it, so
    every IP older than the freshness window is dropped here. Without it a user
    who simply changed network during the window looks like several simultaneous
    devices - the main source of false positives in API mode. Freshness is
    measured against the moment that user was sampled, because a slow fan-out
    can finish minutes after the first users were fetched.

    Returns:
        ``({username: UserType}, stats)``
    """
    from utils.panel_api.online_ips import STALE_EPOCH_FLOOR
    from utils.parse_logs import update_user_device_info_with_node

    sentinel_inbound = str(config_data.get("api_ip_sentinel_inbound") or "API")
    freshness = _resolve_freshness_window(config_data)
    fetch_times = fetch_times or {}
    now = time.time()

    # Flatten first so IP validation runs once per unique IP for the whole
    # cycle instead of once per (user, node, ip) triple.
    per_user_pairs: dict[str, list[tuple[int, str]]] = {}
    raw_ips: set[str] = set()
    nodes_seen: set[int] = set()
    stale_dropped = 0
    future_dropped = 0
    unknown_age = 0
    # Tolerance for ordinary clock skew between panel and limiter.
    future_cutoff = now + max(60.0, float(freshness or 0))

    for username, node_map in payloads.items():
        pairs: list[tuple[int, str]] = []
        stale_cutoff = fetch_times.get(username, now) - freshness
        for node_id, ip_values in node_map.items():
            if node_id in disabled_nodes:
                continue
            nodes_seen.add(node_id)
            for ip, value in ip_values.items():
                if value < STALE_EPOCH_FLOOR:
                    # Too small to be a last-seen Unix timestamp, so this IP's age is
                    # unknown and it cannot be shown to be a current connection. It used
                    # to be read as a legacy connection count and counted as a live
                    # device, which skipped BOTH checks below - and since the panel never
                    # expires an entry, that re-enabled the exact failure the freshness
                    # filter exists to stop: a user who merely changed network looking
                    # like several simultaneous devices. Every value int() cannot parse
                    # lands here too, because _parse_ip_payload coerces those to 1 - an
                    # ISO-8601 datetime string, a float-formatted epoch, a bool.
                    #
                    # Counted as stale on purpose rather than under its own name: if a
                    # panel really did return counts for everything, the existing "every
                    # IP filtered as stale" gate then skips the cycle, instead of
                    # publishing an empty snapshot that clears every pending warning.
                    unknown_age += 1
                    stale_dropped += 1
                    continue
                if value > future_cutoff:
                    # A last-seen stamp cannot be in the future. The panel clock is
                    # ahead (or the value is garbage), which silently turned the
                    # whole freshness filter into a no-op and let the panel's
                    # never-expiring IP list inflate device counts again. Treat it
                    # as unusable: if every value looks like this, the existing
                    # "all IPs stale" gate skips the cycle instead of enforcing.
                    future_dropped += 1
                    stale_dropped += 1
                    continue
                if value < stale_cutoff:
                    stale_dropped += 1
                    continue
                pairs.append((node_id, ip))
                raw_ips.add(ip)
        if pairs:
            per_user_pairs[username] = pairs

    accepted_ips, geo_lookups = await _validate_ips(raw_ips, config_data)

    new_users: dict[str, UserType] = {}
    total_ips = 0
    for username, pairs in per_user_pairs.items():
        usable = [(node_id, ip) for node_id, ip in pairs if ip in accepted_ips]
        if not usable:
            continue
        raw = raw_by_name.get(username) or {}
        # ``status`` stays None: UserType.status is the local UserStatus enum,
        # while the panel's textual status belongs in ``panel_status``.
        user = UserType(
            name=username,
            ip=sorted({ip for _, ip in usable}),
            panel_status=raw.get("status"),
            data_limit=raw.get("data_limit"),
            used_traffic=raw.get("used_traffic"),
            lifetime_used_traffic=raw.get("lifetime_used_traffic"),
            expire=raw.get("expire"),
            group_ids=raw.get("group_ids"),
            online_at=raw.get("online_at"),
            admin_username=_extract_admin_username(raw),
        )
        for node_id, ip in usable:
            await update_user_device_info_with_node(
                user,
                ip,
                sentinel_inbound,
                node_id,
                node_name_map.get(node_id, f"Node-{node_id}"),
            )
        new_users[username] = user
        total_ips += len(user.ip)

    if future_dropped:
        api_ip_logger.warning(
            f"🛰️ {future_dropped} reported IP timestamps were in the future (more than "
            f"{future_cutoff - now:.0f}s ahead) and were discarded - check the clock skew "
            f"between the panel and this host, the freshness filter cannot work while "
            f"the panel clock runs ahead"
        )

    if unknown_age:
        api_ip_logger.warning(
            f"🛰️ {unknown_age} reported IPs carried a value below the epoch floor "
            f"({STALE_EPOCH_FLOOR}), so their age is unknown and they were not counted "
            f"as connected. Either the panel reports connection counts rather than "
            f"last-seen timestamps on this build, or the values are malformed"
        )

    stats = {
        "geo_lookups": geo_lookups,
        "nodes_seen": len(nodes_seen),
        "total_ips": total_ips,
        "users_with_ips": len(new_users),
        "stale_ips": stale_dropped,
        "future_ips": future_dropped,
        "unknown_age_ips": unknown_age,
    }
    return new_users, stats


async def _publish_active_users(new_users: dict[str, UserType]) -> None:
    """
    Replace ``ACTIVE_USERS`` with the freshly collected sample.

    Each API cycle is a complete instantaneous snapshot, so it replaces rather
    than merges. Merging would accumulate stale IPs across cycles and inflate
    device counts — exactly the false-positive risk this mode must avoid.
    """
    async with ACTIVE_USERS_LOCK:
        ACTIVE_USERS.clear()
        ACTIVE_USERS.update(new_users)


def _resolve_online_window(config_data: dict) -> int:
    """
    Freshness window for the candidate query.

    ``api_ip_online_window = 0`` means auto: the check interval plus a 30s
    grace margin, so the window always tracks ``CHECK_INTERVAL`` from ENV/DB
    and no user who connected just after the previous cycle can be missed.
    """
    configured = int(config_data.get("api_ip_online_window") or 0)
    if configured > 0:
        return configured

    interval = config_data.get("check_interval") or (
        config_data.get("monitoring") or {}
    ).get("check_interval", 60)
    try:
        interval = int(float(interval))
    except (ValueError, TypeError):
        interval = 60
    return max(60, interval + 30)


def _resolve_freshness_window(config_data: dict) -> int:
    """
    Age limit for a reported IP, in seconds.

    The panel's online-stats map retains an IP with its last-seen timestamp long
    after the client left, so anything older than this window is not a currently
    connected device. ``api_ip_freshness = 0`` means auto: the check interval
    itself, which is exactly the sample width log mode produces.
    """
    configured = int(config_data.get("api_ip_freshness") or 0)
    if configured > 0:
        return max(30, configured)

    interval = config_data.get("check_interval") or (
        config_data.get("monitoring") or {}
    ).get("check_interval", 60)
    try:
        interval = int(float(interval))
    except (ValueError, TypeError):
        interval = 60
    return max(60, interval)


def _reset_cycle_stats() -> None:
    """Zero the per-cycle counters before a new collection run."""
    for key in LAST_CYCLE_STATS:
        LAST_CYCLE_STATS[key] = "" if key == "skipped_reason" else 0
    LAST_CYCLE_STATS["ran_at"] = time.time()


async def _handle_dead_cycle(config_data: dict, reason: str) -> None:
    """
    Track cycles that produced nothing and revert to log mode if they persist.

    One dead cycle can be a transient panel hiccup, so the fallback only trips
    after ``DEAD_CYCLE_FALLBACK_THRESHOLD`` in a row, and only when
    ``api_ip_auto_fallback`` is enabled.
    """
    global _consecutive_dead_cycles

    _consecutive_dead_cycles += 1
    api_ip_logger.warning(
        f"🛰️ API IP collection produced no data ({reason}) — dead cycle "
        f"{_consecutive_dead_cycles}/{DEAD_CYCLE_FALLBACK_THRESHOLD}"
    )
    if _consecutive_dead_cycles < DEAD_CYCLE_FALLBACK_THRESHOLD:
        return
    if not config_data.get("api_ip_auto_fallback", True):
        return

    from utils.read_config import save_config_value

    if await save_config_value("ip_source", "logs"):
        _consecutive_dead_cycles = 0
        api_ip_logger.error("🛰️ Reverted IP source to log mode after repeated failures")
        await _notify(
            "🛰️ <b>IP Source reverted to Logs</b>\n\n"
            f"API mode returned no usable data for {DEAD_CYCLE_FALLBACK_THRESHOLD} "
            f"consecutive cycles (<code>{reason}</code>).\n"
            "Log streaming has been restored automatically."
        )


async def _handle_coverage_skip(reason: str) -> None:
    """
    Track consecutive cycles skipped for insufficient coverage, and say so.

    A coverage skip is the safe outcome for one cycle - a partial sample would
    clear real offenders' counters. A *run* of them is a different thing: nobody is
    being escalated or banned at all, and the only trace used to be one WARNING per
    cycle that looks identical to the previous one. Reverting to log mode is not the
    remedy (the panel is answering, it is just answering incompletely), so this
    raises an alarm instead of changing the IP source.
    """
    global _consecutive_coverage_skips, _coverage_alert_sent

    _consecutive_coverage_skips += 1
    if _consecutive_coverage_skips < COVERAGE_SKIP_ALERT_THRESHOLD:
        return

    api_ip_logger.error(
        f"⛔ Enforcement has been skipped {_consecutive_coverage_skips} times since the "
        f"last usable sample ({reason}). Nobody is being warned or banned. Check the "
        f"panel's online-stats endpoint, api_ip_timeout and api_ip_concurrency."
    )
    if _coverage_alert_sent:
        return
    _coverage_alert_sent = True
    await _notify(
        "🛰️ <b>API IP mode: enforcement has stopped</b>\n\n"
        f"{_consecutive_coverage_skips} cycles have been skipped since the last usable "
        f"sample because it was incomplete (<code>{reason}</code>).\n"
        "Nobody is being warned or banned while this lasts. Raise "
        "<code>api_ip_timeout</code>, lower <code>api_ip_concurrency</code>, or "
        "switch the IP source back to Logs."
    )


async def _alert_forbidden_once() -> None:
    """Warn the operator the first time the panel rejects online-stats calls."""
    global _forbidden_alert_sent

    if _forbidden_alert_sent:
        return
    _forbidden_alert_sent = True
    await _notify(
        "🛰️ <b>API IP mode: permission denied</b>\n\n"
        "The panel returned <code>403</code> for "
        "<code>/api/node/online_stats/{id}/ip</code>.\n"
        "Grant the panel account the <code>nodes:stats</code> permission, or "
        "switch the IP source back to Logs."
    )


async def collect_active_users_from_api(
    panel_data: PanelType, config_data: dict
) -> bool:
    """
    Collect the connected IPs of monitored users into ``ACTIVE_USERS``.

    This is the API-mode counterpart of the SSE log pipeline and the only
    function ``check_usage`` needs to call. Everything downstream reads the
    same ``ACTIVE_USERS`` structure, so device counting, subnet/CDN grouping,
    the consecutive-warning system, trust scoring and punishment are untouched.

    Returns:
        ``True`` when the sample is trustworthy enough for enforcement to run
        this cycle, ``False`` when the cycle must be skipped. Skipping is the
        safe direction: a partial snapshot would clear the consecutive-violation
        counters of genuine offenders, whereas under-counted IPs can never
        escalate an innocent user into a ban.
    """
    global _consecutive_dead_cycles, _forbidden_alert_sent
    global _consecutive_coverage_skips, _coverage_alert_sent

    from utils.panel_api.online_ips import fetch_online_candidates
    from utils.panel_api.request_helper import is_panel_available

    started = time.perf_counter()
    _reset_cycle_stats()

    if not is_panel_available():
        LAST_CYCLE_STATS["skipped_reason"] = "panel unavailable"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.warning("🛰️ Skipping API IP collection: panel marked unavailable")
        return False

    node_name_map, disabled_nodes, expected_node_ids = await _resolve_node_context(
        panel_data, config_data
    )

    candidate_mode = str(config_data.get("api_ip_candidate_mode") or "online")
    online_window = (
        0 if candidate_mode == "all_monitored" else _resolve_online_window(config_data)
    )

    candidates = await fetch_online_candidates(
        panel_data,
        group_ids=resolve_monitored_group_ids(config_data),
        admin_usernames=resolve_monitored_admins(config_data),
        status="active",
        online_window=online_window,
        page_size=int(config_data.get("api_ip_page_size") or 500),
    )
    # None means the candidate query itself failed. Treating that as "nobody is
    # online" would wipe every pending warning, so the cycle is abandoned.
    if candidates is None:
        LAST_CYCLE_STATS["skipped_reason"] = "candidate query failed"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        await _handle_dead_cycle(config_data, "candidate query failed")
        return False

    LAST_CYCLE_STATS["candidates"] = len(candidates)
    targets = _prefilter_candidates(candidates)
    LAST_CYCLE_STATS["prefiltered"] = len(targets)

    if not targets:
        # A genuinely empty candidate set is a valid result, not a failure:
        # enforcement still has to run so normalized users get cleared.
        #
        # The dead-cycle streak is reset because the candidate query answered, so
        # falling back to log mode is not the remedy. The coverage-skip streak is
        # deliberately left alone: the per-user fan-out never ran this cycle, so it
        # produced no evidence about whether the sample is usable. Clearing it here let
        # an alternating pattern - empty cycle, low-coverage skip, empty cycle - hold
        # the streak at 1 forever, so the "enforcement has stopped" alert could never
        # fire no matter how long enforcement stayed down.
        await _publish_active_users({})
        LAST_CYCLE_STATS["coverage"] = 1.0
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        _consecutive_dead_cycles = 0
        api_ip_logger.info("🛰️ No online candidates this cycle")
        return True

    payloads, counters, fetch_times = await _fetch_all_online_ips(
        panel_data,
        targets,
        concurrency=int(config_data.get("api_ip_concurrency") or 20),
        timeout=float(config_data.get("api_ip_timeout") or 8.0),
    )

    # A 404 is a definitive answer (the user vanished from the panel), so it
    # counts as covered; 403/timeouts/5xx leave the user's state unknown.
    answered = counters["ok"] + counters["not_found"]
    coverage = answered / len(targets) if targets else 1.0

    LAST_CYCLE_STATS.update(
        {
            "fetched_ok": counters["ok"],
            "fetched_failed": counters["failed"],
            "not_found": counters["not_found"],
            "forbidden": counters["forbidden"],
            "coverage": round(coverage, 4),
        }
    )

    if counters["forbidden"]:
        await _alert_forbidden_once()

    if counters["ok"] == 0:
        LAST_CYCLE_STATS["skipped_reason"] = "no successful lookups"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        await _handle_dead_cycle(config_data, "no successful lookups")
        return False

    # An absent key means "use the documented default", not "no floor at all".
    # `or 0.0` silently disabled the guard for any caller that assembled
    # config_data without going through read_config. normalize_min_coverage owns
    # both the default and the percent-versus-fraction rule, so this file no longer
    # keeps its own copy of 0.8 to drift from read_config's.
    min_coverage = normalize_min_coverage(config_data.get("api_ip_min_coverage"))

    if coverage < min_coverage:
        LAST_CYCLE_STATS["skipped_reason"] = (
            f"coverage {coverage:.0%} below {min_coverage:.0%}"
        )
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.warning(
            f"🛰️ Skipping enforcement: coverage {coverage:.1%} < {min_coverage:.0%} "
            f"({counters['ok']} ok, {counters['failed']} failed of {len(targets)})"
        )
        # The dead-cycle streak is deliberately NOT advanced here: the panel did
        # answer, so reverting to log mode is not the right remedy and would thrash
        # the IP source over a slow panel.
        #
        # But a run of these means enforcement has silently stopped - nobody is
        # escalated, nobody is banned - so it gets its own counter and its own
        # alarm rather than being invisible.
        _consecutive_dead_cycles = 0
        await _handle_coverage_skip(LAST_CYCLE_STATS["skipped_reason"])
        return False

    _consecutive_dead_cycles = 0
    _forbidden_alert_sent = False

    raw_by_name = {name: raw for name, _, raw in targets}
    new_users, build_stats = await _build_users_from_payloads(
        payloads, raw_by_name, node_name_map, set(disabled_nodes), config_data,
        fetch_times=fetch_times,
    )

    # Node coverage: how many of the nodes that ought to be reporting actually
    # contributed an IP. The `coverage` gate above is a per-user answer rate and
    # reads 1.0 even when a third of the fleet is silent, so this is the only
    # signal that separates "users happen to be concentrated on a few nodes" from
    # "these nodes stopped reporting" - and the second under-counts devices, which
    # is what lets a real offender's consecutive-violation counter be cleared.
    #
    # Computed before the gates below so a skipped cycle still records it. Leaving it
    # until after them meant the stats screen showed 0/0 on exactly the cycles an
    # operator opens it to diagnose.
    nodes_expected = len(expected_node_ids)
    node_coverage = (build_stats["nodes_seen"] / nodes_expected) if nodes_expected else 1.0
    LAST_CYCLE_STATS["nodes_seen"] = build_stats["nodes_seen"]
    LAST_CYCLE_STATS["nodes_expected"] = nodes_expected
    LAST_CYCLE_STATS["node_coverage"] = round(node_coverage, 4)

    # Every IP filtered out as stale while the panel answered normally means the
    # timestamps cannot be trusted (clock skew between panel and limiter is the
    # usual cause). Publishing the resulting empty snapshot would clear every
    # pending warning, so the cycle is abandoned instead.
    if not new_users and build_stats["stale_ips"] and payloads:
        LAST_CYCLE_STATS.update(build_stats)
        LAST_CYCLE_STATS["skipped_reason"] = "every IP filtered as stale"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.error(
            f"🛰️ Skipping enforcement: all {build_stats['stale_ips']} reported IPs were "
            f"older than the freshness window, or carried a value too small to be a "
            f"timestamp. Check the clock skew between the panel and this container, or "
            f"raise api_ip_freshness."
        )
        # Counted towards the alert streak. Clock skew does not heal by itself, so
        # without this enforcement could stay off indefinitely with nothing but an
        # ERROR line in the container log to show it.
        await _handle_coverage_skip(LAST_CYCLE_STATS["skipped_reason"])
        return False

    # Off unless configured. A genuinely quiet node reports nothing, so the right
    # floor depends on the fleet and has to be observed before it is enforced -
    # guessing one here would skip healthy cycles. The ratio is logged either way,
    # which is the part that was missing.
    min_node_coverage = normalize_min_coverage(
        config_data.get("api_ip_min_node_coverage"), default=0.0
    )
    if min_node_coverage and node_coverage < min_node_coverage:
        # The build already ran, so report what it found. Without this the
        # diagnostics show a cycle that collected nothing, which reads like a dead
        # panel rather than a partial fleet - the opposite of the diagnosis needed.
        LAST_CYCLE_STATS.update(build_stats)
        LAST_CYCLE_STATS["skipped_reason"] = (
            f"node coverage {node_coverage:.0%} below {min_node_coverage:.0%}"
        )
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.error(
            f"🛰️ Skipping enforcement: only {build_stats['nodes_seen']} of "
            f"{nodes_expected} connected nodes reported an IP "
            f"({node_coverage:.1%} < {min_node_coverage:.0%})"
        )
        await _handle_coverage_skip(LAST_CYCLE_STATS["skipped_reason"])
        return False

    await _publish_active_users(new_users)

    # Only here, past every gate: the streak means "cycles skipped since enforcement
    # last actually ran". Clearing it as soon as the fan-out looked healthy meant the
    # two gates below it - all-stale and node coverage - could skip forever without the
    # streak ever passing 1, so their alarm never fired.
    _consecutive_coverage_skips = 0
    _coverage_alert_sent = False

    LAST_CYCLE_STATS.update(build_stats)
    LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)

    api_ip_logger.info(
        f"🛰️ API IP collection: {len(new_users)} users / "
        f"{build_stats['total_ips']} IPs across {build_stats['nodes_seen']} nodes "
        f"(candidates {len(candidates)} → targets {len(targets)}, "
        f"user coverage {coverage:.1%}, node coverage {node_coverage:.1%} "
        f"({build_stats['nodes_seen']}/{nodes_expected}), "
        f"{build_stats['stale_ips']} stale IPs dropped, "
        f"{LAST_CYCLE_STATS['duration_ms']}ms)"
    )
    return True
