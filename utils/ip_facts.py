"""
Single source of truth for what the limiter knows about an IP address.

Both IP sources ask the same questions - is this a real public address, is it a
node's own address, and is it in the country we monitor - and both used to answer
them from their own set of caches. Everything lives here now:

* ``NODE_IPS``     - the nodes' own addresses; never counted as a user device.
* ``COUNTRY_CACHE`` - geo lookup results, bounded and expiring.
* ``VERDICT_CACHE`` - the final accept/reject decision per IP, bounded.

All three are bounded, so a long-running process cannot grow without limit.
"""

import ipaddress
from typing import Optional

from cachetools import TTLCache

from utils.logs import get_logger

ip_facts_logger = get_logger("ip_facts")

# Values that mean "do not filter by country" in the COUNTRY_CODE setting.
GEO_DISABLED_VALUES = {"", "none", "off", "disabled", "any", "all"}

# The nodes' own IPs. Seeded whenever the node list is refreshed; inter-node
# relay traffic must never be attributed to a user.
NODE_IPS: set[str] = set()

# Addresses that are never a user device even when a node forgets to report.
STATIC_BLOCKED_IPS: set[str] = {"1.1.1.1", "8.8.8.8"}

# ip -> ISO country code, 12 hours. Long enough that a busy cycle never repeats
# a lookup, short enough that a reassigned address is re-checked.
COUNTRY_CACHE: TTLCache = TTLCache(maxsize=100_000, ttl=43_200)

# ip -> accepted?, 24 hours. Holds both verdicts so a rejected address is not
# geo-looked-up again on every single cycle.
VERDICT_CACHE: TTLCache = TTLCache(maxsize=100_000, ttl=86_400)


def resolve_country_code(config_data: dict) -> Optional[str]:
    """
    Read the monitored country from the configuration.

    Returns the upper-case ISO code, or ``None`` when country filtering is off.
    Reads ``country_code`` from the root and from the ``monitoring`` mirror, the
    two places ``read_config()`` actually populates.
    """
    raw = str(config_data.get("country_code") or "").strip()
    if not raw:
        raw = str((config_data.get("monitoring") or {}).get("country_code") or "").strip()
    if raw.lower() in GEO_DISABLED_VALUES:
        return None
    return raw.upper()


def register_node_ips(ips) -> int:
    """Add node addresses to the blocklist. Returns how many are known now."""
    for ip in ips:
        if ip:
            NODE_IPS.add(str(ip).strip())
    return len(NODE_IPS)


def is_blocked(ip: str) -> bool:
    """Whether the address belongs to a node or the static blocklist."""
    return ip in NODE_IPS or ip in STATIC_BLOCKED_IPS


def is_public_ip(ip: str) -> bool:
    """Whether the string is a valid, non-private IP address."""
    try:
        return not ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def cached_verdict(ip: str) -> Optional[bool]:
    """Return a previously decided verdict for an IP, or ``None`` if unknown."""
    return VERDICT_CACHE.get(ip)


def remember_verdict(ip: str, accepted: bool) -> None:
    """Record the accept/reject decision for an IP."""
    VERDICT_CACHE[ip] = accepted


# Number of geo lookups that actually reached the network. Callers snapshot this
# to report how much a cycle cost.
_geo_lookups = 0


def geo_lookup_count() -> int:
    """Total geo lookups performed since the process started."""
    return _geo_lookups


async def resolve_country(ip: str) -> Optional[str]:
    """Look up the country of an IP, using the bounded cache first."""
    global _geo_lookups

    cached = COUNTRY_CACHE.get(ip)
    if cached is not None:
        return cached

    from utils.parse_logs import lookup_country

    _geo_lookups += 1
    country = await lookup_country(ip)
    if country:
        COUNTRY_CACHE[ip] = country
    return country


async def is_ip_accepted(ip: str, config_data: dict) -> bool:
    """
    The single admission rule shared by log mode and API mode.

    An address counts as a user device when it is a valid public address, is not
    one of the nodes' own addresses, and - when a country is configured -
    resolves to that country. A failed geo lookup accepts the address: an
    outage at the geo provider must never hide traffic.
    """
    if is_blocked(ip):
        return False

    verdict = VERDICT_CACHE.get(ip)
    if verdict is not None:
        return verdict

    if not is_public_ip(ip):
        VERDICT_CACHE[ip] = False
        return False

    target = resolve_country_code(config_data)
    if target is None:
        VERDICT_CACHE[ip] = True
        return True

    country = await resolve_country(ip)
    if not country:
        # Unknown country: accept, but do not cache the verdict so the address
        # is re-checked once the geo provider recovers.
        return True

    accepted = country.upper() == target
    VERDICT_CACHE[ip] = accepted
    return accepted


def cache_stats() -> dict:
    """Sizes of the bounded caches, for diagnostics."""
    return {
        "node_ips": len(NODE_IPS),
        "country_cache": len(COUNTRY_CACHE),
        "verdict_cache": len(VERDICT_CACHE),
    }
