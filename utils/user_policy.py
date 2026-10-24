"""Derive user eligibility and limits from configuration and factual user fields."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from utils.logs import logger

MIN_EFFECTIVE_LIMIT = 1
DEFAULT_GENERAL_LIMIT = 2


@dataclass(frozen=True)
class UserPolicy:
    """Current policy for a known user, or an explicitly unknown policy."""

    known: bool
    eligible: bool
    limit: int | None
    reason: str
    limit_source: str | None


def _usable_limit(raw: Any, username: str, source: str) -> int | None:
    """Coerce a configured limit, refusing values that ban on the first device."""
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    if value >= MIN_EFFECTIVE_LIMIT:
        return value
    logger.warning(
        f"Ignoring {source} {value!r} for {username}: a limit must be "
        f"{MIN_EFFECTIVE_LIMIT} or greater. Falling back to the next limit in the "
        f"chain - to exempt this user entirely, add them to the whitelist instead."
    )
    return None


def resolve_group_limit(gid: int | str, group_limits: Mapping) -> int | None:
    """Resolve a usable group limit with the existing integer/string key priority."""
    if not group_limits or gid is None:
        return None
    try:
        gid_int = int(gid)
    except (ValueError, TypeError):
        gid_int = None
    if gid_int is not None and gid_int in group_limits:
        raw = group_limits[gid_int]
    elif str(gid) in group_limits:
        raw = group_limits[str(gid)]
    elif gid in group_limits:
        raw = group_limits[gid]
    else:
        return None
    return _usable_limit(raw, str(gid), "group limit")


def get_max_group_limit(group_ids: Sequence[int | str], group_limits: Mapping) -> int | None:
    """Return the maximum usable matching group limit."""
    matching = [limit for gid in (group_ids or []) if (limit := resolve_group_limit(gid, group_limits)) is not None]
    return max(matching) if matching else None


def _calculate_user_limit(
    *,
    username: str,
    config: Mapping[str, Any],
    facts: Mapping[str, Any] | None,
    special_limits: Mapping[str, Any] | None = None,
    batched_group_limits: Mapping[str, Any] | None = None,
) -> tuple[int, str]:
    """Share limit calculation with legacy reporting wrappers; ignore derived fields."""
    limits = config.get("limits")
    if special_limits is None:
        special_limits = limits.get("special", {}) if isinstance(limits, Mapping) else {}
    if username in special_limits:
        limit = _usable_limit(special_limits[username], username, "special limit")
        if limit is not None:
            return limit, "special"
    if batched_group_limits and username in batched_group_limits:
        limit = _usable_limit(batched_group_limits[username], username, "group limit")
        if limit is not None:
            return limit, "group"
    limit = get_max_group_limit((facts or {}).get("group_ids", []), config.get("group_limits", {}))
    if limit is not None:
        return limit, "group"

    general = DEFAULT_GENERAL_LIMIT
    if isinstance(limits, Mapping) and "general" in limits:
        general = limits["general"]
    elif "general_limit" in config:
        general = config["general_limit"]
    try:
        general = int(general)
    except (ValueError, TypeError):
        return DEFAULT_GENERAL_LIMIT, "general"
    if general >= MIN_EFFECTIVE_LIMIT:
        return general, "general"
    logger.critical(
        f"⛔ Configured general limit {general!r} is not a usable limit - a "
        f"limit below {MIN_EFFECTIVE_LIMIT} would ban every active user. Using the "
        f"default of {DEFAULT_GENERAL_LIMIT} instead. Set GENERAL_LIMIT to 1 or more, "
        f"and use the whitelist to exempt users."
    )
    return DEFAULT_GENERAL_LIMIT, "general"


def evaluate_user_policy(*, username: str, config: Mapping[str, Any], facts: Mapping[str, Any] | None) -> UserPolicy:
    """Apply exact whitelist, group and admin predicates to a known user's facts."""
    if facts is None:
        return UserPolicy(False, False, None, "unknown", None)
    limit, source = _calculate_user_limit(username=username, config=config, facts=facts)
    if username in config.get("except_users", []):
        return UserPolicy(True, False, limit, "whitelisted", source)

    group_filter = config.get("group_filter", {})
    if group_filter.get("enabled", False):
        filter_ids = {str(gid) for gid in group_filter.get("group_ids", [])}
        matches = any(str(gid) in filter_ids for gid in (facts.get("group_ids") or []))
        admitted = matches if group_filter.get("mode", "include") == "include" else not matches
        if not admitted:
            return UserPolicy(True, False, limit, "group_filtered", source)

    admin_filter = config.get("admin_filter", {})
    names = admin_filter.get("admin_usernames", [])
    owner = facts.get("owner_username")
    if admin_filter.get("enabled", False) and names and owner is not None:
        matches = owner in names
        mode = admin_filter.get("mode", "include")
        if (mode == "include" and not matches) or (mode == "exclude" and matches):
            return UserPolicy(True, False, limit, "admin_filtered", source)
    return UserPolicy(True, True, limit, "eligible", source)
