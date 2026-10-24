"""
Configuration module for PG-Limiter.
Reads settings from:
- Environment variables (.env) for static settings
- Database for dynamic settings that can be changed via Telegram

The merged result is held in a single process-wide dict and rebuilt when a local
write commits or a refresh discovers another process's write. ``read_config``
hands out a deep copy of it, so a caller that
edits what it got cannot reach the shared configuration every other task is
reading - see the note on the cache below.
"""

import asyncio
import copy
import math
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, AsyncIterator, Dict, List, Optional

from utils.logs import get_logger

# Module logger
config_logger = get_logger("read_config")

# Try to import database module
try:
    from db import get_db, ConfigCRUD, UserCRUD

    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

# Single in-process configuration cache. ``None`` means "not loaded yet".
#
# Never hand this object out. It used to be returned directly, and every caller in
# the process therefore shared one mutable dict with no TTL: a Telegram handler doing
# ``config_data["disabled_nodes"].append(id)`` changed what the enforcement loop saw,
# permanently, with nothing in the log. Two such bugs were fixed one caller at a time
# before the pattern recurred in eight more places, so the guarantee now lives here
# instead: ``read_config`` deep-copies on the way out. Read
# ``read_config_scalar`` for the one case that does not need a copy.
_config_cache: Optional[Dict[str, Any]] = None
_config_revision = 0
_config_cache_db_revision: int | None = None
MAX_CONFIG_LOAD_ATTEMPTS = 3
_config_read_lock = asyncio.Lock()
_config_write_lock = asyncio.Lock()


def get_config_revision() -> int:
    """Identify settings changes across asynchronous collection and preparation."""
    return _config_revision


# Throttle for the database-failure traceback below. A degraded configuration is never
# cached, so while the database is down every read retries and would log again.
DB_FAILURE_LOG_INTERVAL = 60.0
_db_failure_logged_at = -DB_FAILURE_LOG_INTERVAL


def _invalidate_config_cache() -> None:
    """Drop local data synchronously after a committed root transaction."""
    global _config_cache, _config_revision, _config_cache_db_revision
    _config_revision += 1
    _config_cache = None
    _config_cache_db_revision = None


async def invalidate_config_cache():
    """Drop local settings and retire the process's previous evidence generation."""
    _invalidate_config_cache()
    config_logger.info("🔧 Configuration cache invalidated")


def _parse_admin_ids(admin_ids_str: str) -> List[int]:
    """Parse comma-separated admin IDs into list of integers."""
    if not admin_ids_str:
        return []
    try:
        return [int(id.strip()) for id in admin_ids_str.split(",") if id.strip()]
    except ValueError:
        return []


def _get_env(key: str, default: Any = None, cast_type: type = str) -> Any:
    """Get environment variable with type casting."""
    value = os.environ.get(key, default)
    if value is None or value == "":
        return default

    if cast_type == bool:
        return str(value).lower() in ("true", "1", "yes", "on")
    elif cast_type == int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    elif cast_type == float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    return value


# The coverage floor for API mode, as a fraction. This is the one place the default
# lives; ip_source_api and the settings screen import it rather than repeating 0.8.
DEFAULT_API_IP_MIN_COVERAGE = 0.8
DEFAULT_API_IP_FRESHNESS = 60


def normalize_api_ip_freshness(raw: Any, default: int = DEFAULT_API_IP_FRESHNESS) -> int:
    """Accept exact positive integer seconds, with no interval or minimum clamp."""
    text = str(raw).strip()
    if text.isascii() and text.isdecimal() and int(text) > 0:
        return int(text)
    if raw is not None:
        config_logger.error(f"Invalid API IP freshness {raw!r}; using {default}s")
    return default


def normalize_min_coverage(raw: Any, default: float = DEFAULT_API_IP_MIN_COVERAGE) -> float:
    """
    Turn a configured coverage floor into a fraction between 0 and 1.

    Accepts both spellings, because the operator sets this as a percentage in the
    environment (``API_IP_MIN_COVERAGE=80``) while the database row and every
    comparison use a fraction (``0.8``):

        80   -> 0.8      100 -> 1.0      150 -> 1.0 (clamped)
        0.8  -> 0.8      1   -> 1.0      -5  -> 0.0 (clamped)
        0    -> 0.0, which switches the gate off entirely

    Anything above 1 is read as a percentage, anything at or below 1 as a fraction.
    The ambiguous value 1 is taken as 100%, the stricter of the two readings: getting
    this wrong in the other direction would silently disable the guard that stops a
    thin sample from clearing real offenders' violation counters. A value that is not
    a number at all falls back to the default rather than to zero, for the same reason.
    """
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (ValueError, TypeError):
        config_logger.error(
            f"⚠️ Ignoring API_IP_MIN_COVERAGE / api_ip_min_coverage value {raw!r}: not a "
            f"number - falling back to {default:.0%}"
        )
        return default
    if not math.isfinite(value):
        # "nan" and "inf" survive float(), and nan in particular defeats the clamp
        # below because every comparison against it is False. Both would land on a
        # floor of 1.0, which stalls enforcement silently rather than obviously.
        config_logger.error(
            f"⚠️ Ignoring API_IP_MIN_COVERAGE / api_ip_min_coverage value {raw!r}: not a "
            f"finite number - falling back to {default:.0%}"
        )
        return default
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value))


# The general limit applies to every user with no special or group limit, so an
# unusable value here is the widest possible blast radius: a limit below 1 makes the
# first device a violation and bans the whole installation after the usual consecutive
# scans. Exemption is the whitelist, never a limit of zero.
DEFAULT_GENERAL_LIMIT = 2


def normalize_general_limit(raw: Any, source: str, default: int = DEFAULT_GENERAL_LIMIT) -> int:
    """
    Coerce a configured general limit, falling back to ``default`` when unusable.

    ``default`` lets the database row fall back to the environment value rather than to
    the built-in, which is the precedence GENERAL_LIMIT and CHECK_INTERVAL already use.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        config_logger.error(f"⚠️ Ignoring {source} value {raw!r}: not a whole number - using {default}")
        return default
    if value >= 1:
        return value
    config_logger.critical(
        f"⛔ Ignoring {source} value {value!r}: a general limit below 1 would ban every "
        f"active user on their first device. Using {default} instead - to exempt users, "
        f"add them to the whitelist."
    )
    return default


def load_env_config() -> Dict[str, Any]:
    """Load configuration from environment variables."""
    return {
        # Panel settings (from ENV only)
        "panel": {
            "domain": _get_env("PANEL_DOMAIN", ""),
            "username": _get_env("PANEL_USERNAME", "admin"),
            "password": _get_env("PANEL_PASSWORD", ""),
        },
        # Telegram settings (from ENV only)
        "telegram": {
            "bot_token": _get_env("BOT_TOKEN", ""),
            "admins": _parse_admin_ids(_get_env("ADMIN_IDS", "")),
        },
        # Limiter settings (from ENV - defaults)
        "limits": {
            "general": normalize_general_limit(os.environ.get("GENERAL_LIMIT"), "GENERAL_LIMIT"),
            "special": {},  # Loaded from DB
        },
        "except_users": [],  # Loaded from DB
        # Monitoring settings (from ENV)
        "monitoring": {
            "check_interval": _get_env("CHECK_INTERVAL", 60, int),
            "max_warning_count": _get_env("MAX_WARNING_COUNT", 3, int),
            "time_to_active_users": _get_env("TIME_TO_ACTIVE_USERS", 900, int),
            "country_code": _get_env("COUNTRY_CODE", ""),
        },
        "check_interval": _get_env("CHECK_INTERVAL", 60, int),
        # These describe the Xray process/container clock, never the limiter host.
        # Preserve invalid input so Logs readiness fails closed instead of guessing.
        "log_timezone": os.environ.get("LOG_TIMEZONE"),
        "log_node_timezones": os.environ.get("LOG_NODE_TIMEZONES", "{}"),
        "log_replay_cache_size": os.environ.get("LOG_REPLAY_CACHE_SIZE", "10000"),
        "max_warning_count": _get_env("MAX_WARNING_COUNT", 3, int),
        "time_to_active_users": _get_env("TIME_TO_ACTIVE_USERS", 900, int),
        "country_code": _get_env("COUNTRY_CODE", ""),
        # User sync settings
        "user_sync_interval": _get_env("USER_SYNC_INTERVAL", 5, int),  # Minutes
        # Minimum share of candidates that must answer before an API-mode cycle is
        # allowed to enforce. Set as a percentage; see normalize_min_coverage.
        "api_ip_min_coverage": normalize_min_coverage(os.environ.get("API_IP_MIN_COVERAGE")),
        # Share of the connected node fleet that must report before an API-mode cycle
        # enforces, as a percentage. 0 (the default) switches the gate off; a genuinely
        # quiet node reports nothing, so the right floor has to be observed on a real
        # fleet before it is enforced.
        "api_ip_min_node_coverage": normalize_min_coverage(os.environ.get("API_IP_MIN_NODE_COVERAGE"), default=0.0),
        "api_ip_freshness": normalize_api_ip_freshness(os.environ.get("API_IP_FRESHNESS")),
        # API settings (from ENV)
        "api": {
            "enabled": _get_env("API_ENABLED", False, bool),
            "host": _get_env("API_HOST", "0.0.0.0"),
            "port": _get_env("API_PORT", 8080, int),
            "username": _get_env("API_USERNAME", "admin"),
            "password": _get_env("API_PASSWORD", ""),
        },
        # Database
        "database_url": _get_env("DATABASE_URL", "sqlite+aiosqlite:///./data/pg_limiter.db"),
    }


async def load_db_config(usernames: Iterable[str] = ()) -> Dict[str, Any]:
    """Load dynamic configuration from database."""
    if not DB_AVAILABLE:
        return {}

    try:
        async with get_db() as session:
            from sqlalchemy import text
            from db.crud.config_revision import ConfigRevisionCRUD

            # sqlite3 legacy SELECTs do not begin a physical read transaction.
            if session.get_bind().dialect.name == "sqlite":
                await session.execute(text("BEGIN"))
            db_revision = await ConfigRevisionCRUD.read(session)
            user_facts = await UserCRUD.get_policy_facts(session, usernames)
            # Load all config from database
            db_config = await ConfigCRUD.get_all(session)

            # Load special limits
            special_limits = await UserCRUD.get_all_special_limits(session)

            # Load except users
            except_users = await UserCRUD.get_all_excepted(session)

        return {
            "db_config": db_config,
            "db_revision": db_revision,
            "user_facts": user_facts,
            "special_limits": special_limits,
            "except_users": except_users,
        }
    except Exception as error:  # pylint: disable=broad-except
        # Silence here was dangerous. An empty dict makes read_config build a config
        # with no whitelist, no special limits, no group limits and group_filter
        # disabled - and a disabled group filter means "limit every user". One
        # transient SQLite error could therefore put every monitored user under the
        # general limit with no exceptions, and the result was cached for the life of
        # the process with nothing in the log to show it.
        #
        # Throttled, because a degraded configuration is deliberately never cached: while
        # the database is down every read rebuilds, and the SSE loop alone asks ~600
        # times per cycle. Un-throttled that buried the actual failure under hundreds of
        # identical tracebacks. The first one is full; after that it is one line a minute.
        global _db_failure_logged_at

        now = time.monotonic()
        if now - _db_failure_logged_at > DB_FAILURE_LOG_INTERVAL:
            _db_failure_logged_at = now
            config_logger.exception(
                f"❌ Could not load dynamic configuration from the database: {error}. "
                f"Whitelist, special limits and group limits are unknown, so this "
                f"configuration must not be used for enforcement."
            )
        else:
            config_logger.error(
                f"❌ Database configuration still unavailable: {error} "
                f"(traceback throttled to once every {int(DB_FAILURE_LOG_INTERVAL)}s)"
            )
        return {"_load_failed": True}


def get_config_sync() -> Dict[str, Any]:
    """Get configuration synchronously (ENV only, for startup)."""
    return load_env_config()


@dataclass(frozen=True)
class ConfigSnapshot:
    """Private settings and user facts read from one committed database view."""

    config: dict[str, Any]
    db_revision: int
    local_revision: int
    user_facts: dict[str, dict[str, Any]]


async def _read_db_revision() -> int:
    from db.crud.config_revision import ConfigRevisionCRUD

    async with get_db() as session:
        return await ConfigRevisionCRUD.read(session)


async def read_config(check_required_elements: bool = False, *, refresh: bool = False) -> Dict[str, Any]:
    """Read private settings, optionally checking other processes' commits."""
    async with _config_read_lock:
        return await _load_config(check_required_elements, refresh=refresh)


async def read_config_snapshot(*, usernames: Iterable[str] = ()) -> ConfigSnapshot:
    """Read coherent current settings and facts for a scan or final policy check."""
    async with _config_read_lock:
        return await _load_config(refresh=True, usernames=tuple(usernames), snapshot=True)


async def _load_config(check_required_elements=False, *, refresh=False, usernames=(), snapshot=False):
    global _config_cache, _config_cache_db_revision

    if _config_cache is not None and not check_required_elements and not snapshot:
        if not refresh:
            return copy.deepcopy(_config_cache)
        cached = _config_cache
        cached_db_revision = _config_cache_db_revision
        cached_local_revision = get_config_revision()
        try:
            current_db_revision = await _read_db_revision()
            if (
                cached_db_revision == current_db_revision
                and _config_cache is cached
                and cached_local_revision == get_config_revision()
            ):
                return copy.deepcopy(cached)
        except Exception:
            # Never serve cached policy as current when shared state is unavailable.
            pass
        if _config_cache is not None:
            _invalidate_config_cache()

    config = None
    db_data = {}
    for _attempt in range(MAX_CONFIG_LOAD_ATTEMPTS):
        loaded_revision = get_config_revision()
        # Ordinary calls keep the no-argument loader contract used by callers/tests.
        db_data = await load_db_config(usernames) if usernames else await load_db_config()
        config = await _merge_loaded_config(load_env_config(), db_data, check_required_elements)
        if config.get("config_degraded"):
            break
        db_revision = db_data.get("db_revision")
        if snapshot and _config_cache_db_revision is not None and _config_cache_db_revision != db_revision:
            # A factual snapshot must retire evidence from the previous settings too.
            _invalidate_config_cache()
        if loaded_revision != get_config_revision():
            continue
        if db_revision is not None:
            try:
                if db_revision != await _read_db_revision():
                    _invalidate_config_cache()
                    continue
            except Exception:
                config["config_degraded"] = True
                break
        if loaded_revision != get_config_revision():
            continue
        config["_revision"] = loaded_revision
        _config_cache = config
        _config_cache_db_revision = db_revision
        if snapshot:
            return ConfigSnapshot(
                copy.deepcopy(config), db_revision or 0, loaded_revision, copy.deepcopy(db_data.get("user_facts", {}))
            )
        return copy.deepcopy(config)
    config["config_degraded"] = True
    _config_cache = None
    _config_cache_db_revision = None
    config_logger.error("Configuration unavailable or changed repeatedly; enforcement must wait")
    if snapshot:
        return ConfigSnapshot(config, db_data.get("db_revision", -1), get_config_revision(), {})
    return config


async def _merge_loaded_config(env_config, db_data, check_required_elements=False):
    """Preserve existing ENV/DB normalization without publishing a partial load."""
    # Merge configurations
    config = env_config.copy()

    # Propagate a failed database read (see load_db_config) so the enforcement loop can
    # refuse to act on a configuration that has no whitelist and no group limits.
    config["config_degraded"] = bool(db_data.get("_load_failed"))

    # Add special limits from DB
    if "special_limits" in db_data:
        config["limits"]["special"] = db_data["special_limits"]

    # Add except users from DB
    if "except_users" in db_data:
        config["except_users"] = db_data["except_users"]

    # Merge DB config (dynamic settings changeable via Telegram)
    db_config = db_data.get("db_config", {})

    # Merge monitoring and timing settings from DB
    if "check_interval" in db_config:
        try:
            config["check_interval"] = int(db_config["check_interval"])
        except (ValueError, TypeError):
            pass
    if "time_to_active_users" in db_config:
        try:
            config["time_to_active_users"] = int(db_config["time_to_active_users"])
        except (ValueError, TypeError):
            pass
    if "max_warning_count" in db_config:
        try:
            config["max_warning_count"] = int(db_config["max_warning_count"])
        except (ValueError, TypeError):
            pass
    if "country_code" in db_config:
        config["country_code"] = str(db_config["country_code"])

    # Synchronize monitoring sub-dictionary
    config["monitoring"] = {
        "check_interval": config["check_interval"],
        "max_warning_count": config["max_warning_count"],
        "time_to_active_users": config["time_to_active_users"],
        "country_code": config["country_code"],
    }

    if "general_limit" in db_config:
        config["limits"]["general"] = normalize_general_limit(
            db_config["general_limit"],
            "general_limit",
            default=config["limits"]["general"],
        )

    config["disable_method"] = db_config.get("disable_method", "status")
    config["disabled_group_id"] = db_config.get("disabled_group_id")
    if config["disabled_group_id"]:
        try:
            config["disabled_group_id"] = int(config["disabled_group_id"])
        except (ValueError, TypeError):
            config["disabled_group_id"] = None

    config["fallback_group_id"] = db_config.get("fallback_group_id")
    if config["fallback_group_id"]:
        try:
            config["fallback_group_id"] = int(config["fallback_group_id"])
        except (ValueError, TypeError):
            config["fallback_group_id"] = None

    config["enhanced_details"] = db_config.get("enhanced_details", "true").lower() == "true"
    config["show_single_ip_users"] = db_config.get("show_single_ip_users", "false").lower() == "true"
    config["ipinfo_token"] = db_config.get("ipinfo_token", "")

    # Punishment system settings
    config["punishment"] = {
        "enabled": db_config.get("punishment_enabled", "true").lower() == "true",
        "window_hours": int(db_config.get("punishment_window_hours", "168")),
        "steps": [],
    }
    # Load punishment steps from DB (stored as JSON string)
    punishment_steps_str = db_config.get("punishment_steps", "")
    if punishment_steps_str:
        try:
            import json

            config["punishment"]["steps"] = json.loads(punishment_steps_str)
        except (json.JSONDecodeError, ValueError):
            pass

    # Group filter settings
    config["group_filter"] = {
        "enabled": db_config.get("group_filter_enabled", "false").lower() == "true",
        "mode": db_config.get("group_filter_mode", "include"),
        "group_ids": [],
    }
    group_ids_str = db_config.get("group_filter_ids", "")
    if group_ids_str:
        try:
            config["group_filter"]["group_ids"] = [int(x.strip()) for x in group_ids_str.split(",") if x.strip()]
        except ValueError:
            pass

    # Admin filter settings
    config["admin_filter"] = {
        "enabled": db_config.get("admin_filter_enabled", "false").lower() == "true",
        "mode": db_config.get("admin_filter_mode", "include"),
        "admin_usernames": [],
    }
    admin_usernames_str = db_config.get("admin_filter_usernames", "")
    if admin_usernames_str:
        config["admin_filter"]["admin_usernames"] = [x.strip() for x in admin_usernames_str.split(",") if x.strip()]

    # Group limits settings - mapping of group IDs to connection limits
    config["group_limits"] = {}
    group_limits_str = db_config.get("group_limits", "")
    if group_limits_str:
        try:
            import json

            parsed_limits = json.loads(group_limits_str)
            if isinstance(parsed_limits, dict):
                normalized_limits = {}
                for k, v in parsed_limits.items():
                    try:
                        normalized_limits[int(k)] = int(v)
                    except (ValueError, TypeError):
                        pass
                config["group_limits"] = normalized_limits
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # CDN mode settings - list of inbound protocols that should be treated as CDN
    # When an inbound is in CDN mode, all IPs from that inbound count as 1 device
    config["cdn_inbounds"] = []
    cdn_inbounds_str = db_config.get("cdn_inbounds", "")
    if cdn_inbounds_str:
        config["cdn_inbounds"] = [x.strip() for x in cdn_inbounds_str.split(",") if x.strip()]

    # CDN provider type (cloudflare, custom)
    config["cdn_provider"] = db_config.get("cdn_provider", "cloudflare")

    # Whether to use X-Forwarded-For to extract real IP for CDN inbounds
    config["cdn_use_xff"] = db_config.get("cdn_use_xff", "true").lower() == "true"

    # CDN nodes - list of node IDs that are behind CDN
    # All IPs from CDN nodes count as 1 device (similar to cdn_inbounds but per-node)
    config["cdn_nodes"] = []
    cdn_nodes_str = db_config.get("cdn_nodes", "")
    if cdn_nodes_str:
        try:
            config["cdn_nodes"] = [int(x.strip()) for x in cdn_nodes_str.split(",") if x.strip()]
        except ValueError:
            pass

    # Subnet IP Grouping - relaxed mode where IPs in the same /24 or /16 subnet
    # that use the same node AND inbound are counted as a single device
    config["subnet_ip_grouping"] = db_config.get("subnet_ip_grouping", "false").lower() == "true"
    config["subnet_grouping_mode"] = db_config.get("subnet_grouping_mode", "/24")
    if config["subnet_grouping_mode"] not in ["/24", "/16"]:
        config["subnet_grouping_mode"] = "/24"

    # High Trust IP Grouping - for users with high trust score, multiple IPs
    # using the SAME node AND inbound are counted as one device
    # (detects WiFi/Mobile switching on same device)
    config["high_trust_ip_grouping"] = db_config.get("high_trust_ip_grouping", "false").lower() == "true"

    # Minimum trust score required for high_trust_ip_grouping to apply
    try:
        config["high_trust_threshold"] = int(db_config.get("high_trust_threshold", "20"))
    except (ValueError, TypeError):
        config["high_trust_threshold"] = 20

    # Device counting mode. node_id is never part of the device key: several
    # nodes can serve the same core config, so one client is registered on all
    # of them simultaneously.
    #   "device" -> the inbound stays in the key (default). One IP reaching two
    #               inbounds counts as two devices, which is how several people
    #               sharing a single connection are detected.
    #   "ip"     -> the inbound is dropped too: one client IP is exactly one
    #               device. Subnet grouping still applies when enabled; High
    #               Trust grouping has no effect (IP counting is already the
    #               most lenient key).
    config["device_count_mode"] = str(db_config.get("device_count_mode", "device")).strip().lower()
    if config["device_count_mode"] not in ("device", "ip"):
        config["device_count_mode"] = "device"

    # Disabled nodes - list of node IDs to exclude from monitoring
    # Connections from these nodes are completely ignored
    config["disabled_nodes"] = []
    disabled_nodes_str = db_config.get("disabled_nodes", "")
    if disabled_nodes_str:
        try:
            config["disabled_nodes"] = [int(x.strip()) for x in disabled_nodes_str.split(",") if x.strip()]
        except ValueError:
            pass

    # User sync interval (in minutes)
    if "user_sync_interval" in db_config:
        try:
            config["user_sync_interval"] = int(db_config["user_sync_interval"])
        except (ValueError, TypeError):
            pass

    # ------------------------------------------------------------------
    # IP source selection: "logs" (SSE log streaming) or "api" (panel API)
    # In "api" mode the connected IPs are pulled from the panel's
    # online-stats endpoints instead of parsing node logs. The rest of the
    # pipeline (device counting, warning system, punishment) is identical.
    # ------------------------------------------------------------------
    config["ip_source"] = str(db_config.get("ip_source", "logs")).strip().lower()
    if config["ip_source"] not in ("logs", "api"):
        config["ip_source"] = "logs"

    # Max concurrent per-user online-IP requests during the API fan-out.
    # Sized against the shared httpx pool (max_connections=50).
    try:
        config["api_ip_concurrency"] = int(db_config.get("api_ip_concurrency", "20"))
    except (ValueError, TypeError):
        config["api_ip_concurrency"] = 20
    config["api_ip_concurrency"] = max(1, min(40, config["api_ip_concurrency"]))

    # Legacy keys retained for backup compatibility. The collector always uses
    # online=true; neither these keys nor CHECK_INTERVAL widen its candidates.
    config["api_ip_candidate_mode"] = str(db_config.get("api_ip_candidate_mode", "online")).strip().lower()
    if config["api_ip_candidate_mode"] not in ("online", "all_monitored"):
        config["api_ip_candidate_mode"] = "online"

    # Legacy online-window value; no longer used by API collection.
    try:
        config["api_ip_online_window"] = int(db_config.get("api_ip_online_window", "0"))
    except (ValueError, TypeError):
        config["api_ip_online_window"] = 0
    if config["api_ip_online_window"] < 0:
        config["api_ip_online_window"] = 0

    # Stored positive values override ENV. Legacy Auto becomes a durable value
    # once, so changing the collection interval cannot change the acceptance age.
    freshness_raw = db_config.get("api_ip_freshness")
    legacy_auto = str(freshness_raw).strip().lower() in ("0", "auto")
    legacy_auto = legacy_auto or (
        freshness_raw is None and "ip_source" in db_config and not os.environ.get("API_IP_FRESHNESS")
    )
    if legacy_auto and not config.get("config_degraded"):
        config["api_ip_freshness"] = max(60, int(config.get("check_interval") or 60))
        if not await save_config_value("api_ip_freshness", config["api_ip_freshness"]):
            config["config_degraded"] = True
    else:
        config["api_ip_freshness"] = normalize_api_ip_freshness(freshness_raw, default=config["api_ip_freshness"])

    # Page size for the candidate /api/users query
    try:
        config["api_ip_page_size"] = int(db_config.get("api_ip_page_size", "500"))
    except (ValueError, TypeError):
        config["api_ip_page_size"] = 500
    config["api_ip_page_size"] = max(50, min(1000, config["api_ip_page_size"]))

    # Timeout for a single per-user online-IP request
    try:
        config["api_ip_timeout"] = float(db_config.get("api_ip_timeout", "8.0"))
    except (ValueError, TypeError):
        config["api_ip_timeout"] = 8.0
    config["api_ip_timeout"] = max(2.0, min(60.0, config["api_ip_timeout"]))

    # Placeholder inbound protocol name: the panel API returns {ip: timestamp}
    # per node without any inbound information, so a sentinel is used to
    # keep the (ip, inbound) device-counting key shape intact.
    config["api_ip_sentinel_inbound"] = str(db_config.get("api_ip_sentinel_inbound", "API")).strip() or "API"

    # Minimum successful-fetch ratio required to run enforcement for a cycle.
    # Below this the cycle is skipped entirely so that a flaky panel cannot
    # mass-reset the consecutive-violation counters of real offenders.
    #
    # The environment value already went through normalize_min_coverage in
    # load_env_config, so it is the fallback here and a database row still wins - the
    # same precedence GENERAL_LIMIT and CHECK_INTERVAL use.
    config["api_ip_min_coverage"] = normalize_min_coverage(
        db_config.get("api_ip_min_coverage"),
        default=config.get("api_ip_min_coverage", DEFAULT_API_IP_MIN_COVERAGE),
    )

    # Fraction of the expected node fleet that must appear in the API payloads.
    # ip_source_api reads this key, but nothing ever populated it, so the gate was
    # permanently off: the "only 16 of 49 nodes reported" cycles passed as healthy.
    # Default stays 0.0 (off) so wiring it up cannot change behaviour on its own.
    #
    # Shares normalize_min_coverage with the user-coverage floor so both accept the same
    # spellings. The plain float() this used to do read a hand-written 80 as 80, clamped
    # it to 100%, and would then have skipped every cycle - the trap being that the only
    # way to set this was to write the row by hand in the first place.
    config["api_ip_min_node_coverage"] = normalize_min_coverage(
        db_config.get("api_ip_min_node_coverage"),
        default=config.get("api_ip_min_node_coverage", 0.0),
    )

    # Automatically fall back to log mode after repeated total failures
    # (e.g. the panel account is missing the nodes:stats permission).
    config["api_ip_auto_fallback"] = db_config.get("api_ip_auto_fallback", "true").lower() == "true"

    # Validate required elements
    if check_required_elements:
        if not config["panel"]["domain"]:
            raise ValueError("PANEL_DOMAIN is not set in environment")
        if not config["panel"]["password"]:
            raise ValueError("PANEL_PASSWORD is not set in environment")
        if not config["telegram"]["bot_token"]:
            raise ValueError("BOT_TOKEN is not set in environment")
        if not config["telegram"]["admins"]:
            raise ValueError("ADMIN_IDS is not set in environment")

    # A configuration whose database half could not be read is missing the whitelist,
    # the special limits and the group limits. It is served (so the bot and the CLI
    # keep working) but flagged and never cached, so the next call retries instead of
    # freezing the degraded view in for the life of the process. check_usage refuses to
    # enforce while the flag is set.
    if config.get("config_degraded"):
        config_logger.critical(
            "⛔ Serving a configuration with no database settings - enforcement will be "
            "skipped until a database read succeeds"
        )
        # No copy needed: this one is deliberately never cached, so it is already
        # private to this caller and nobody else holds a reference to it.
        return config

    return config


async def read_config_scalar(key: str, default: Any = None) -> Any:
    """
    Read one immutable top-level setting without copying the whole configuration.

    ``read_config`` deep-copies on every call, which is the right default but is
    wasted work for a caller that wants a single string or number. The SSE log loop
    is the case that matters: it asks whether the IP source was switched to API mode
    once every 15 seconds per node, so on a 49-node fleet with a 180s check interval
    that is ~588 reads per cycle for one string.

    Only immutable values may be read this way. Handing out a list or a dict from the
    cache is exactly the sharing bug the deep copy exists to prevent, so that raises
    instead of quietly returning a shared reference.
    """
    cache = _config_cache
    if cache is None:
        # Cold cache: first call, or the first call after an invalidation. Build it and
        # read from what came back, because a degraded configuration is deliberately
        # not cached and would leave the global at None on the next line.
        cache = await read_config()

    value = cache.get(key, default)
    if isinstance(value, (dict, list, set)):
        raise TypeError(
            f"read_config_scalar({key!r}) may only be used for immutable values, but "
            f"{key!r} holds a {type(value).__name__}. Use read_config() instead - "
            f"returning the container itself would share the process-wide cache."
        )
    return value


@asynccontextmanager
async def config_write_transaction() -> AsyncIterator[None]:
    """Serialize a caller-owned settings transaction through cache invalidation.

    Enter this before opening the database transaction and leave it after commit.
    Keep reads of merged config and notifications outside this context. Source
    selection itself uses save_config_value/delete_config_value for observation
    cleanup as well as serialization.
    """
    async with _config_write_lock:
        try:
            yield
        finally:
            # Failed transactions can leave commit status uncertain too.
            await invalidate_config_cache()


async def _finish_source_change() -> None:
    """Finish committed source cleanup before another writer can proceed.

    Cancellation may arrive while the active-user lock is held by a reader.
    Keep the writer lock until cleanup finishes, then propagate cancellation.
    """
    from utils.shared_state import discard_source_observations

    cleanup = asyncio.create_task(discard_source_observations())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def save_config_value(key: str, value: Any, *, expected_revision: int | None = None) -> bool | None:
    """Save a setting, optionally rejecting a superseded collection's write.

    Ordinary callers receive True for a committed write or False for failure.
    Guarded callers also receive None if newer settings prevented the attempt.
    Successful source writes discard old observations before another writer runs.
    """
    async with _config_write_lock:
        if expected_revision is not None and expected_revision != get_config_revision():
            return None
        if not DB_AVAILABLE:
            config_logger.error(f"❌ Cannot save configuration {key!r}: the database module is unavailable")
            return False

        expected_db_revision = _config_cache_db_revision if expected_revision is not None else None
        if expected_revision is not None and expected_db_revision is None:
            # There is no committed configuration version for this evidence to compare.
            return None
        try:
            async with get_db() as session:
                if expected_db_revision is None:
                    await ConfigCRUD.set(session, key, str(value))
                elif await ConfigCRUD.set(session, key, str(value), expected_db_revision=expected_db_revision) is None:
                    return None
        except Exception as error:  # pylint: disable=broad-except
            config_logger.exception(
                f"❌ Failed to save configuration {key!r}: {error}. The previous value is still in effect."
            )
            return False
        finally:
            # A failed or cancelled database operation can have an uncertain commit
            # outcome. Reload rather than continuing to serve potentially stale data.
            await invalidate_config_cache()
        if key == "ip_source":
            await _finish_source_change()
        return True


async def delete_config_value(key: str) -> bool:
    """Delete a setting under the same serialization as configuration saves."""
    async with _config_write_lock:
        if not DB_AVAILABLE:
            config_logger.error(f"❌ Cannot delete configuration {key!r}: the database module is unavailable")
            return False

        try:
            async with get_db() as session:
                await ConfigCRUD.delete(session, key)
        except Exception as error:  # pylint: disable=broad-except
            config_logger.exception(
                f"❌ Failed to delete configuration {key!r}: {error}. The previous value is still in effect."
            )
            return False
        finally:
            await invalidate_config_cache()
        if key == "ip_source":
            await _finish_source_change()
        return True


async def get_config_value_from_db(key: str, default: Any = None) -> Any:
    """
    Get a single config value straight from the database.

    A read failure returns ``default``, which is indistinguishable from "not set" -
    so it is logged rather than swallowed.
    """
    if not DB_AVAILABLE:
        return default

    try:
        async with get_db() as session:
            value = await ConfigCRUD.get(session, key, default)
            return value
    except Exception as error:  # pylint: disable=broad-except
        config_logger.error(
            f"⚠️ Could not read configuration {key!r} from the database: {error}. "
            f"Falling back to {default!r}, which reads the same as 'not set'."
        )
        return default


def get_config_value(config: dict, key: str, default: Any = None) -> Any:
    """
    Get config value by key name.
    Supports both old flat keys and new structure.
    """
    key_map = {
        "PANEL_DOMAIN": lambda c: c.get("panel", {}).get("domain"),
        "PANEL_USERNAME": lambda c: c.get("panel", {}).get("username"),
        "PANEL_PASSWORD": lambda c: c.get("panel", {}).get("password"),
        "BOT_TOKEN": lambda c: c.get("telegram", {}).get("bot_token"),
        "ADMINS": lambda c: c.get("telegram", {}).get("admins"),
        "GENERAL_LIMIT": lambda c: c.get("limits", {}).get("general"),
        "SPECIAL_LIMIT": lambda c: c.get("limits", {}).get("special"),
        "SPECIAL_LIMITS": lambda c: c.get("limits", {}).get("special"),
        "GROUP_LIMITS": lambda c: c.get("group_limits"),
        "EXCEPT_USERS": lambda c: c.get("except_users"),
        "CHECK_INTERVAL": lambda c: c.get("check_interval"),
        "TIME_TO_ACTIVE_USERS": lambda c: c.get("time_to_active_users"),
        "COUNTRY_CODE": lambda c: c.get("country_code"),
        "IP_LOCATION": lambda c: c.get("country_code"),  # Alias
        "DISABLE_METHOD": lambda c: c.get("disable_method"),
        "DISABLED_GROUP_ID": lambda c: c.get("disabled_group_id"),
        "FALLBACK_GROUP_ID": lambda c: c.get("fallback_group_id"),
        "ENHANCED_DETAILS": lambda c: c.get("enhanced_details"),
        "SHOW_SINGLE_IP_USERS": lambda c: c.get("show_single_ip_users"),
        "IPINFO_TOKEN": lambda c: c.get("ipinfo_token"),
        "IP_SOURCE": lambda c: c.get("ip_source"),
        "DEVICE_COUNT_MODE": lambda c: c.get("device_count_mode"),
    }

    if key in key_map:
        value = key_map[key](config)
        return value if value is not None else default

    return config.get(key, default)


# Compatibility aliases
async def get_config(*args, **kwargs):
    """Alias for read_config for backward compatibility."""
    return await read_config(*args, **kwargs)
