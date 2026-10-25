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
import copy
import time
from dataclasses import dataclass, replace
from html import escape
from typing import Optional

from utils.logs import get_logger
from utils.read_config import get_config_revision, normalize_api_ip_freshness, normalize_min_coverage
from utils.shared_state import (
    APISampleEvidence,
    ActiveUsersSnapshot,
    begin_api_collection,
    api_evidence_is_current,
    publish_api_snapshot,
)
from utils.monitoring_gate import monitoring_is_current, monitoring_snapshot, synchronize_monitoring
from utils.types import ConnectionInfo, PanelType, UserType
from utils.device_count import COUNT_MODE_NODE

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
    "node_evidence_evaluated": False,
    "node_coverage": 0.0,
    "duration_ms": 0,
    "geo_lookups": 0,
    "stale_ips": 0,
    # Declared here so _reset_cycle_stats zeroes them: it iterates the live dict, and a
    # key that only ever arrives via update(build_stats) would keep the previous cycle's
    # value on any cycle that returns before the build.
    "future_ips": 0,
    "unknown_age_ips": 0,
    # Reasons can overlap; each counter counts affected users, not IPs or nodes.
    "missing_node_users": 0,
    "null_node_users": 0,
    "unknown_age_users": 0,
    "future_timestamp_users": 0,
    "missing_node_ids": (),
    "skipped_reason": "",
    # Separate remote collection health from the bool returned to enforcement:
    # complete users may still be judged during a partial collection.
    "collection_outcome": "failure",  # success, partial, failure, superseded
    "unknown_users": 0,
    "consecutive_unsuccessful_cycles": 0,
    "fallback_applied": False,
}

# Unsuccessful collection cycles, scoped to the configuration that produced them.
_consecutive_dead_cycles = 0
_failure_revision: int | None = None
_collection_lock = asyncio.Lock()
_forbidden_alert_sent = False

# Separate diagnostics for repeated enforcement skips, including when automatic
# fallback is disabled or cannot be persisted.
_consecutive_coverage_skips = 0
_coverage_alert_sent = False

# Consecutive dead cycles tolerated before reverting to log mode
DEAD_CYCLE_FALLBACK_THRESHOLD = 2

# Consecutive coverage skips tolerated before saying enforcement has stopped
COVERAGE_SKIP_ALERT_THRESHOLD = 3


def get_last_cycle_stats() -> dict:
    """Return a copy of the most recent collection cycle statistics."""
    return dict(LAST_CYCLE_STATS)


def format_api_evidence_summary(stats: dict) -> str:
    """Describe aggregate lookup and evidence results without identifying users."""

    def count(key: str) -> int:
        value = stats.get(key, 0)
        return value if type(value) is int and value >= 0 else 0

    seen, expected = count("nodes_seen"), count("nodes_expected")
    if expected and stats.get("node_evidence_evaluated"):
        reporting = f"<code>{seen}/{expected}</code> ({seen / expected:.1%})"
        absent = f"<code>{max(0, expected - seen)}</code>"
    else:
        reporting = f"unknown (expected <code>{expected}</code>)" if expected else "unknown (expected fleet unknown)"
        absent = "unknown"
    return (
        f"HTTP lookups: <code>{count('fetched_ok')}</code> OK / <code>{count('fetched_failed')}</code> failed\n"
        f"Forbidden lookups (403): <code>{count('forbidden')}</code>\n"
        f"Users with incomplete evidence: <code>{count('unknown_users')}</code>\n"
        f"Users missing required node evidence: <code>{count('missing_node_users')}</code>\n"
        f"Nodes reporting across responses: {reporting}\n"
        f"Nodes absent across all responses: {absent}\n"
        f"Stale source observations dropped: <code>{count('stale_ips')}</code>"
    )


async def _notify(message: str) -> None:
    """Best-effort Telegram notification; never raises into the collector."""
    try:
        if not monitoring_snapshot().allowed:
            return
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
    Every relevant enabled node must be connected before even an empty candidate
    list can establish known absence; the panel IP producer omits unhealthy nodes.
    """
    from utils.ip_facts import register_node_ips
    from utils.panel_api.nodes import get_nodes

    node_name_map: dict[int, str] = {}
    connected_node_ids: set = set()
    disabled_nodes = set(config_data.get("disabled_nodes") or [])
    node_mode = config_data.get("device_count_mode") == COUNT_MODE_NODE
    try:
        nodes = await get_nodes(panel_data)
    except Exception as error:
        api_ip_logger.warning(f"Could not resolve node list: {error}")
        nodes = None

    if nodes and not isinstance(nodes, ValueError):
        register_node_ips(node.node_ip for node in nodes)
        for node in nodes:
            if node_mode and type(node.node_id) is not int:
                raise ValueError("Invalid node ID in the authoritative node list")
            node_name_map[node.node_id] = node.node_name
            if node_mode and node.status in ("disabled", "limited"):
                disabled_nodes.add(node.node_id)
            if getattr(node, "status", None) == "connected":
                connected_node_ids.add(node.node_id)
            elif node.node_id not in disabled_nodes:
                raise ValueError("Enabled node availability is unknown")

    if nodes is None or isinstance(nodes, ValueError):
        raise ValueError("Node availability is unknown")
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
    results = await asyncio.gather(*[_decide(ip) for ip in raw_ips], return_exceptions=True)
    for item in results:
        if isinstance(item, asyncio.CancelledError):
            raise item
        if isinstance(item, tuple) and item[1]:
            accepted.add(item[0])

    return accepted, geo_lookup_count() - lookups_before


class OnlineIPResults(dict[str, dict[int, dict[str, int] | None]]):
    """Fan-out payloads with definitive deleted-user answers kept separately."""

    def __init__(self) -> None:
        super().__init__()
        self.not_found_users: set[str] = set()


async def _fetch_all_online_ips(
    panel_data: PanelType,
    targets: list[tuple[str, int, dict]],
    concurrency: int,
    timeout: float,
) -> tuple[OnlineIPResults, dict[str, int], dict[str, float]]:
    """
    Run the bounded-concurrency fan-out over the candidate users.

    Returns:
        ``({username: {node_id: {ip: last_seen}}}, counters, {username: fetched_at})``

    ``fetched_at`` is receipt time for diagnostics. Eligibility uses the source
    timestamp at evaluation, including after the entire fan-out completes.
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
    admission = monitoring_snapshot()
    payloads = OnlineIPResults()
    fetch_times: dict[str, float] = {}
    if not targets or not monitoring_is_current(admission):
        return payloads, counters, fetch_times

    token = await resolve_panel_token(panel_data)
    if not token or not monitoring_is_current(admission):
        counters["failed"] = len(targets)
        return payloads, counters, fetch_times

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(username: str, user_id: int, active_token: str):
        async with semaphore:
            if not monitoring_is_current(admission):
                return None
            payload, outcome = await fetch_user_online_ips(panel_data, user_id, active_token, timeout=timeout)
            if not monitoring_is_current(admission):
                return None
            return username, user_id, payload, outcome, time.time()

    async def _run(batch: list[tuple[str, int]], active_token: str) -> list[tuple[str, int]]:
        """Execute one pass; return the subset that needs a token refresh."""
        needs_retry: list[tuple[str, int]] = []
        results = await asyncio.gather(
            *[_one(name, uid, active_token) for name, uid in batch],
            return_exceptions=True,
        )
        for item in results:
            if isinstance(item, asyncio.CancelledError):
                raise item
            if not isinstance(item, tuple):
                counters["failed"] += 1
                continue
            username, user_id, payload, outcome, fetched_at = item
            if outcome == OUTCOME_OK:
                counters["ok"] += 1
                payloads[username] = payload if payload is not None else {}
                fetch_times[username] = fetched_at
            elif outcome == OUTCOME_NOT_FOUND:
                counters["not_found"] += 1
                payloads.not_found_users.add(username)
                fetch_times[username] = fetched_at
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
    if retry_batch and monitoring_is_current(admission):
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
    payloads: dict[str, dict[int, dict[str, int] | None]],
    raw_by_name: dict[str, dict],
    node_name_map: dict[int, str],
    disabled_nodes: set,
    config_data: dict,
    fetch_times: dict[str, float] | None = None,
    expected_node_ids: set[int] | None = None,
) -> tuple[dict[str, UserType], dict]:
    """Build source-timestamp observations and retain each user's uncertainty.

    Receipt times are retained in the transport interface for diagnostics only.
    Xray timestamps are evaluated after asynchronous IP admission, and again
    after enforcement preparation; an old connection never gets a new timestamp.
    """
    from utils.panel_api.online_ips import STALE_EPOCH_FLOOR

    freshness = _resolve_freshness_window(config_data)
    sentinel = str(config_data.get("api_ip_sentinel_inbound") or "API")
    raw_ips = set()
    pairs_by_user = {}
    unknown = set()
    unknown_by_reason = {
        "missing_node_users": set(),
        "null_node_users": set(),
        "unknown_age_users": set(),
        "future_timestamp_users": set(),
    }
    missing_node_ids = set()
    nodes_seen = set()
    stats = {
        "geo_lookups": 0,
        "nodes_seen": 0,
        "total_ips": 0,
        "users_with_ips": 0,
        "stale_ips": 0,
        "future_ips": 0,
        "unknown_age_ips": 0,
    }
    now = time.time()
    for username, node_map in payloads.items():
        if config_data.get("device_count_mode") == COUNT_MODE_NODE:
            # Unknown identities cannot supply a required map, even when bools
            # or floats compare equal to an integer node ID in Python.
            node_map = {
                node_id: values
                for node_id, values in node_map.items()
                if type(node_id) is int and expected_node_ids is not None and node_id in expected_node_ids
            }
            if expected_node_ids is None:
                unknown.add(username)
                unknown_by_reason["missing_node_users"].add(username)
        if expected_node_ids and not expected_node_ids.issubset(node_map):
            unknown.add(username)
            unknown_by_reason["missing_node_users"].add(username)
            missing_node_ids.update(node_id for node_id in expected_node_ids - node_map.keys() if type(node_id) is int)
        pairs = []
        for node_id, ip_values in node_map.items():
            if node_id in disabled_nodes:
                continue
            if ip_values is None:
                unknown.add(username)
                unknown_by_reason["null_node_users"].add(username)
                continue
            if expected_node_ids is None or node_id in expected_node_ids:
                nodes_seen.add(node_id)
            for ip, stamp in ip_values.items():
                if type(stamp) is not int or stamp < STALE_EPOCH_FLOOR:
                    unknown.add(username)
                    unknown_by_reason["unknown_age_users"].add(username)
                    stats["unknown_age_ips"] += 1
                    continue
                if stamp > now:
                    unknown.add(username)
                    unknown_by_reason["future_timestamp_users"].add(username)
                    stats["future_ips"] += 1
                    continue
                pairs.append((node_id, ip, stamp))
                raw_ips.add(ip)
        pairs_by_user[username] = pairs

    accepted, stats["geo_lookups"] = await _validate_ips(raw_ips, config_data)
    evaluated_at = time.time()
    users = ActiveUsersSnapshot()
    users.unknown_users = frozenset(unknown)
    for username, pairs in pairs_by_user.items():
        usable = []
        for node_id, ip, stamp in pairs:
            age = evaluated_at - stamp
            if age < 0:
                unknown.add(username)
                unknown_by_reason["future_timestamp_users"].add(username)
                stats["future_ips"] += 1
            elif age >= freshness:
                stats["stale_ips"] += 1
            elif ip in accepted:
                usable.append((node_id, ip, stamp))
        if not usable or username in unknown:
            continue
        raw = raw_by_name.get(username) or {}
        user = UserType(
            name=username,
            ip=sorted({ip for _, ip, _ in usable}),
            panel_status=raw.get("status"),
            data_limit=raw.get("data_limit"),
            used_traffic=raw.get("used_traffic"),
            lifetime_used_traffic=raw.get("lifetime_used_traffic"),
            expire=raw.get("expire"),
            group_ids=raw.get("group_ids"),
            online_at=raw.get("online_at"),
            admin_username=_extract_admin_username(raw),
        )
        for node_id, ip, stamp in usable:
            user.device_info.connections.append(
                ConnectionInfo(
                    ip=ip,
                    node_id=node_id,
                    node_name=node_name_map.get(node_id, f"Node-{node_id}"),
                    inbound_protocol=sentinel,
                    last_seen=stamp,
                )
            )
        user.device_info.unique_ips = set(user.ip)
        user.device_info.unique_nodes = {node_id for node_id, _, _ in usable}
        user.device_info.inbound_protocols = {sentinel}
        user.device_info.is_multi_device = len(user.ip) > 1
        users[username] = user
    users.unknown_users = frozenset(unknown)
    stats.update(
        nodes_seen=len(nodes_seen), total_ips=sum(len(u.ip) for u in users.values()), users_with_ips=len(users)
    )
    stats.update({reason: len(names) for reason, names in unknown_by_reason.items()})
    stats["missing_node_ids"] = tuple(sorted(missing_node_ids))
    return users, stats


def _resolve_online_window(_config_data: dict) -> int:
    """Compatibility value selecting online=true, independent of the interval."""
    return 110


def _resolve_freshness_window(config_data: dict) -> int:
    """Read exact normalized seconds; only read_config migrates legacy Auto."""
    return normalize_api_ip_freshness(config_data.get("api_ip_freshness"))


def _reset_cycle_stats() -> None:
    """Zero the per-cycle counters before a new collection run."""
    for key in LAST_CYCLE_STATS:
        LAST_CYCLE_STATS[key] = "" if key == "skipped_reason" else 0
    LAST_CYCLE_STATS.update(node_evidence_evaluated=False, fallback_applied=False)
    LAST_CYCLE_STATS["missing_node_ids"] = ()
    LAST_CYCLE_STATS["ran_at"] = time.time()
    LAST_CYCLE_STATS["collection_outcome"] = "failure"


def retire_api_collection_state(reason: str = "") -> None:
    """Do not carry old source failures or observations through a Topics pause."""
    global _consecutive_dead_cycles, _failure_revision, _consecutive_coverage_skips
    global _coverage_alert_sent, _forbidden_alert_sent

    _consecutive_dead_cycles = 0
    _failure_revision = None
    _consecutive_coverage_skips = 0
    _coverage_alert_sent = False
    _forbidden_alert_sent = False
    _reset_cycle_stats()
    LAST_CYCLE_STATS.update(
        ran_at=0, collection_outcome="superseded", skipped_reason=reason or "Topics monitoring changed"
    )


@dataclass(frozen=True)
class _CollectionResult:
    """A cycle owns its health outcome independently of the diagnostic stats."""

    outcome: str
    enforcement_ready: bool
    reason: str
    evidence: APISampleEvidence


async def _account_collection_cycle(config_data: dict, result: _CollectionResult) -> bool:
    """Account once and commit fallback without overwriting newer settings."""
    global _consecutive_dead_cycles, _failure_revision

    revision = result.evidence.config_revision
    if _failure_revision != revision:
        _consecutive_dead_cycles = 0
        _failure_revision = revision
    if result.outcome == "success":
        _consecutive_dead_cycles = 0
        LAST_CYCLE_STATS["consecutive_unsuccessful_cycles"] = 0
        return result.enforcement_ready

    _consecutive_dead_cycles += 1
    LAST_CYCLE_STATS["consecutive_unsuccessful_cycles"] = _consecutive_dead_cycles
    api_ip_logger.warning(
        f"🛰️ API IP collection incomplete ({result.reason}) — unsuccessful cycle "
        f"{_consecutive_dead_cycles}/{DEAD_CYCLE_FALLBACK_THRESHOLD}"
    )
    if _consecutive_dead_cycles < DEAD_CYCLE_FALLBACK_THRESHOLD or not config_data.get("api_ip_auto_fallback", True):
        return result.enforcement_ready

    from utils.read_config import save_config_value

    saved = await save_config_value("ip_source", "logs", expected_revision=revision)
    if saved is False:
        # Failed attempts also invalidate config. Preserve this attempt's streak,
        # but never transfer it across a later manual settings/source write.
        _failure_revision = get_config_revision()
        return result.enforcement_ready
    _consecutive_dead_cycles = 0
    _failure_revision = None
    if saved is None:
        return False

    LAST_CYCLE_STATS["fallback_applied"] = True
    api_ip_logger.error("🛰️ Reverted IP source to log mode after repeated failures")
    await _notify(
        "🛰️ <b>IP Source reverted to Logs</b>\n\n"
        f"{LAST_CYCLE_STATS['consecutive_unsuccessful_cycles']} consecutive API collections were incomplete.\n"
        f"Latest reason: <code>{escape(result.reason)}</code>.\n\n"
        f"{format_api_evidence_summary(LAST_CYCLE_STATS)}\n\n"
        "HTTP success does not confirm complete node evidence for each user.\n"
        "The latest API enforcement cycle was skipped; warning counters and history were preserved.\n"
        "Logs were selected automatically. Fresh Logs evidence is required before enforcement resumes."
    )
    return False


async def _handle_coverage_skip(reason: str) -> None:
    """
    Track consecutive cycles skipped for insufficient coverage, and say so.

    A coverage skip is the safe outcome for one cycle - a partial sample would
    clear real offenders' counters. A *run* of them is a different thing: nobody is
    being escalated or banned at all, and the only trace used to be one WARNING per
    cycle that looks identical to the previous one. This diagnostic remains useful
    when automatic fallback is disabled or its configuration write fails.
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


async def collect_active_users_from_api(panel_data: PanelType, config_data: dict) -> bool:
    """Collect one API sample and account its outcome once before enforcement.

    Partial samples can still enforce known users while API remains selected.
    A committed fallback discards the sample and returns False. Cancellation,
    degraded local settings and superseded evidence never count as API outages.
    """
    admission = monitoring_snapshot()
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return False
    async with _collection_lock:
        if not monitoring_is_current(admission):
            return False
        source_config = config_data
        config_data = copy.deepcopy(config_data)
        if config_data.get("config_degraded") or config_data.get("ip_source", "api") != "api":
            _reset_cycle_stats()
            LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="API configuration unavailable")
            return False
        evidence = APISampleEvidence(
            begin_api_collection(),
            config_data.get("_revision", get_config_revision()),
            _resolve_freshness_window(config_data),
        )
        started = time.perf_counter()
        try:
            result = await _collect_api_cycle(panel_data, config_data, source_config, evidence)
        finally:
            LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        if not monitoring_is_current(admission):
            await synchronize_monitoring()
            return False
        if not api_evidence_is_current(result.evidence) or source_config != config_data:
            LAST_CYCLE_STATS.update(
                collection_outcome="superseded", skipped_reason="configuration or collection changed"
            )
            return False
        LAST_CYCLE_STATS["collection_outcome"] = result.outcome
        if result.outcome == "superseded":
            return False
        return await _account_collection_cycle(config_data, result)


async def _collect_api_cycle(
    panel_data: PanelType, config_data: dict, source_config: dict, evidence: APISampleEvidence
) -> _CollectionResult:
    """Build a cycle-owned result; global stats are only for diagnostics."""
    global _forbidden_alert_sent, _consecutive_coverage_skips, _coverage_alert_sent

    from utils.panel_api.online_ips import fetch_online_candidates
    from utils.panel_api.request_helper import is_panel_available

    started = time.perf_counter()
    admission = monitoring_snapshot()
    _reset_cycle_stats()
    if not api_evidence_is_current(evidence) or not monitoring_is_current(admission):
        LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="configuration changed")
        return _CollectionResult("superseded", False, "configuration or collection changed", evidence)

    if not is_panel_available():
        LAST_CYCLE_STATS["skipped_reason"] = "panel unavailable"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.warning("🛰️ Skipping API IP collection: panel marked unavailable")
        return _CollectionResult("failure", False, "panel unavailable", evidence)

    try:
        node_name_map, disabled_nodes, expected_node_ids = await _resolve_node_context(panel_data, config_data)
    except ValueError:
        LAST_CYCLE_STATS["skipped_reason"] = "node availability unknown"
        return _CollectionResult("failure", False, "node availability unknown", evidence)
    LAST_CYCLE_STATS["nodes_expected"] = len(expected_node_ids)
    evidence = replace(evidence, eligible_node_ids=frozenset(expected_node_ids))

    if not monitoring_is_current(admission):
        return _CollectionResult("superseded", False, "Topics monitoring paused or changed", evidence)

    online_window = _resolve_online_window(config_data)

    candidates = await fetch_online_candidates(
        panel_data,
        group_ids=resolve_monitored_group_ids(config_data),
        admin_usernames=resolve_monitored_admins(config_data),
        status="active",
        online_window=online_window,
        page_size=int(config_data.get("api_ip_page_size") or 500),
    )
    if not api_evidence_is_current(evidence) or source_config != config_data or not monitoring_is_current(admission):
        LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="configuration or collection changed")
        return _CollectionResult("superseded", False, "configuration or collection changed", evidence)
    # None means the candidate query itself failed. Treating that as "nobody is
    # online" would wipe every pending warning, so the cycle is abandoned.
    if candidates is None:
        LAST_CYCLE_STATS["skipped_reason"] = "candidate query failed"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return _CollectionResult("failure", False, "candidate query failed", evidence)

    LAST_CYCLE_STATS["candidates"] = len(candidates)
    targets = _prefilter_candidates(candidates)
    group_filter = config_data.get("group_filter") or {}
    if (
        group_filter.get("enabled")
        and group_filter.get("mode", "include") == "include"
        and not group_filter.get("group_ids")
    ):
        targets = []
    LAST_CYCLE_STATS["prefiltered"] = len(targets)

    if not targets:
        # Valid absence still runs enforcement so normalized users can clear.
        if not await publish_api_snapshot({}, evidence):
            return _CollectionResult("superseded", False, "configuration or collection changed", evidence)
        LAST_CYCLE_STATS["collection_outcome"] = "success"
        LAST_CYCLE_STATS["coverage"] = 1.0
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.info("🛰️ No online candidates this cycle")
        return _CollectionResult("success", True, "", evidence)

    payloads, counters, fetch_times = await _fetch_all_online_ips(
        panel_data,
        targets,
        concurrency=int(config_data.get("api_ip_concurrency") or 20),
        timeout=float(config_data.get("api_ip_timeout") or 8.0),
    )

    if not api_evidence_is_current(evidence) or source_config != config_data or not monitoring_is_current(admission):
        LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="configuration or collection changed")
        return _CollectionResult("superseded", False, "configuration or collection changed", evidence)

    # A 404 is a definitive answer (the user vanished from the panel), so it
    # counts as covered; 403/timeouts/5xx leave the user's state unknown.
    answered = counters["ok"] + counters["not_found"]
    coverage = answered / len(targets) if targets else 1.0
    answered_users = set(payloads) | getattr(payloads, "not_found_users", set())
    transport_unknown_users = {name for name, _, _ in targets} - answered_users

    LAST_CYCLE_STATS.update(
        {
            "fetched_ok": counters["ok"],
            "fetched_failed": counters["failed"],
            "not_found": counters["not_found"],
            "forbidden": counters["forbidden"],
            "coverage": round(coverage, 4),
            "unknown_users": len(transport_unknown_users),
        }
    )

    if counters["forbidden"]:
        await _alert_forbidden_once()
        if not monitoring_is_current(admission):
            return _CollectionResult("superseded", False, "Topics monitoring paused or changed", evidence)

    if answered == 0:
        LAST_CYCLE_STATS["skipped_reason"] = "no successful lookups"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return _CollectionResult("failure", False, "no successful lookups", evidence)

    # An absent key means "use the documented default", not "no floor at all".
    # `or 0.0` silently disabled the guard for any caller that assembled
    # config_data without going through read_config. normalize_min_coverage owns
    # both the default and the percent-versus-fraction rule, so this file no longer
    # keeps its own copy of 0.8 to drift from read_config's.
    min_coverage = normalize_min_coverage(config_data.get("api_ip_min_coverage"))

    if coverage < min_coverage:
        LAST_CYCLE_STATS["collection_outcome"] = "partial"
        LAST_CYCLE_STATS["skipped_reason"] = f"coverage {coverage:.0%} below {min_coverage:.0%}"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.warning(
            f"🛰️ Skipping enforcement: coverage {coverage:.1%} < {min_coverage:.0%} "
            f"({counters['ok']} ok, {counters['failed']} failed of {len(targets)})"
        )
        reason = f"coverage {coverage:.0%} below {min_coverage:.0%}"
        await _handle_coverage_skip(reason)
        return _CollectionResult("partial", False, reason, evidence)

    _forbidden_alert_sent = False

    raw_by_name = {name: raw for name, _, raw in targets}
    new_users, build_stats = await _build_users_from_payloads(
        payloads,
        raw_by_name,
        node_name_map,
        set(disabled_nodes),
        config_data,
        fetch_times=fetch_times,
        expected_node_ids=expected_node_ids,
    )
    LAST_CYCLE_STATS["node_evidence_evaluated"] = True

    # Node coverage counts a healthy empty map as a response too. Individual
    # users with missing nodes are protected separately; this aggregate is for
    # diagnostics and the optional fleet-wide coverage gate.
    #
    # Computed before the gates below so a skipped cycle still records it. Leaving it
    # until after them meant the stats screen showed 0/0 on exactly the cycles an
    # operator opens it to diagnose.
    nodes_expected = len(expected_node_ids)
    node_coverage = (build_stats["nodes_seen"] / nodes_expected) if nodes_expected else 1.0
    LAST_CYCLE_STATS["nodes_seen"] = build_stats["nodes_seen"]
    LAST_CYCLE_STATS["nodes_expected"] = nodes_expected
    LAST_CYCLE_STATS["node_coverage"] = round(node_coverage, 4)

    unknown_users = (set(raw_by_name) - answered_users) | set(new_users.unknown_users)
    if not api_evidence_is_current(evidence) or source_config != config_data or not monitoring_is_current(admission):
        LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="configuration or collection changed")
        return _CollectionResult("superseded", False, "configuration or collection changed", evidence)
    LAST_CYCLE_STATS["unknown_users"] = len(unknown_users)
    LAST_CYCLE_STATS.update(build_stats)
    if new_users.unknown_users:
        api_ip_logger.warning(
            "🛰️ API user evidence unavailable: %s missing-node, %s null-node, %s undatable, "
            "%s future-dated users; missing node IDs: %s",
            build_stats["missing_node_users"],
            build_stats["null_node_users"],
            build_stats["unknown_age_users"],
            build_stats["future_timestamp_users"],
            build_stats["missing_node_ids"],
        )
    if unknown_users == set(raw_by_name):
        LAST_CYCLE_STATS["collection_outcome"] = "partial"
        LAST_CYCLE_STATS.update(build_stats)
        LAST_CYCLE_STATS["skipped_reason"] = "all user evidence unknown"
        await _handle_coverage_skip("all user evidence unknown")
        return _CollectionResult("partial", False, "all user evidence unknown", evidence)

    # Preserve the optional aggregate gate. It never makes an individual user's
    # missing-node evidence complete, even when the configured floor is zero.
    min_node_coverage = normalize_min_coverage(config_data.get("api_ip_min_node_coverage"), default=0.0)
    # Deleted users need no node observation; their absence is definitive even
    # if every candidate disappeared between the list and per-user requests.
    if payloads and min_node_coverage and node_coverage < min_node_coverage:
        # The build already ran, so report what it found. Without this the
        # diagnostics show a cycle that collected nothing, which reads like a dead
        # panel rather than a partial fleet - the opposite of the diagnosis needed.
        LAST_CYCLE_STATS.update(build_stats)
        LAST_CYCLE_STATS["skipped_reason"] = f"node coverage {node_coverage:.0%} below {min_node_coverage:.0%}"
        LAST_CYCLE_STATS["duration_ms"] = int((time.perf_counter() - started) * 1000)
        api_ip_logger.error(
            f"🛰️ Skipping enforcement: only {build_stats['nodes_seen']} of "
            f"{nodes_expected} connected nodes returned an IP map "
            f"({node_coverage:.1%} < {min_node_coverage:.0%})"
        )
        reason = f"node coverage {node_coverage:.0%} below {min_node_coverage:.0%}"
        await _handle_coverage_skip(reason)
        return _CollectionResult("partial", False, reason, evidence)

    evidence = replace(evidence, unknown_users=frozenset(unknown_users))
    if not await publish_api_snapshot(new_users, evidence):
        LAST_CYCLE_STATS.update(collection_outcome="superseded", skipped_reason="configuration or collection changed")
        return _CollectionResult("superseded", False, "configuration or collection changed", evidence)

    # Only here, past every gate: the streak means "cycles skipped since enforcement
    # last actually ran". Clearing it as soon as the fan-out looked healthy meant the
    # evidence and node-coverage gates could skip forever without the
    # streak ever passing 1, so their alarm never fired.
    _consecutive_coverage_skips = 0
    _coverage_alert_sent = False

    LAST_CYCLE_STATS.update(build_stats)
    LAST_CYCLE_STATS["collection_outcome"] = "partial" if unknown_users else "success"
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
    return _CollectionResult(
        "partial" if unknown_users else "success", True, "incomplete user evidence" if unknown_users else "", evidence
    )
