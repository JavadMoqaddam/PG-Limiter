"""
Configuration module for PG-Limiter.
Reads settings from:
- Environment variables (.env) for static settings
- Database for dynamic settings that can be changed via Telegram
The merged result is held in a single process-wide dict and rebuilt only when a
setting is written, so a read is a plain dictionary lookup.
"""

import os
from typing import Any, Dict, List, Optional

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
_config_cache: Optional[Dict[str, Any]] = None


async def invalidate_config_cache():
    """Drop the cached configuration so the next read rebuilds it from ENV + DB."""
    global _config_cache

    _config_cache = None
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
            "general": _get_env("GENERAL_LIMIT", 2, int),
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
        "max_warning_count": _get_env("MAX_WARNING_COUNT", 3, int),
        "time_to_active_users": _get_env("TIME_TO_ACTIVE_USERS", 900, int),
        "country_code": _get_env("COUNTRY_CODE", ""),
        # User sync settings
        "user_sync_interval": _get_env("USER_SYNC_INTERVAL", 5, int),  # Minutes
        # API settings (from ENV)
        "api": {
            "enabled": _get_env("API_ENABLED", False, bool),
            "host": _get_env("API_HOST", "0.0.0.0"),
            "port": _get_env("API_PORT", 8080, int),
            "username": _get_env("API_USERNAME", "admin"),
            "password": _get_env("API_PASSWORD", ""),
        },
        # Database
        "database_url": _get_env(
            "DATABASE_URL",
            "sqlite+aiosqlite:///./data/pg_limiter.db"
        ),
    }


async def load_db_config() -> Dict[str, Any]:
    """Load dynamic configuration from database."""
    if not DB_AVAILABLE:
        return {}
    
    try:
        async with get_db() as session:
            # Load all config from database
            db_config = await ConfigCRUD.get_all(session)
            
            # Load special limits
            special_limits = await UserCRUD.get_all_special_limits(session)
            
            # Load except users
            except_users = await UserCRUD.get_all_excepted(session)
        
        return {
            "db_config": db_config,
            "special_limits": special_limits,
            "except_users": except_users,
        }
    except Exception as error:  # pylint: disable=broad-except
        # Silence here was dangerous. An empty dict makes read_config build a config
        # with no whitelist, no special limits, no group limits and group_filter
        # disabled - and a disabled group filter means "limit every user"
        # (user_group_filter.should_limit_user returns True when it is off). One
        # transient SQLite error could therefore put every monitored user under the
        # general limit with no exceptions, and the result was cached for the life of
        # the process with nothing in the log to show it.
        config_logger.exception(
            f"❌ Could not load dynamic configuration from the database: {error}. "
            f"Whitelist, special limits and group limits are unknown, so this "
            f"configuration must not be used for enforcement."
        )
        return {"_load_failed": True}


def get_config_sync() -> Dict[str, Any]:
    """Get configuration synchronously (ENV only, for startup)."""
    return load_env_config()


async def read_config(check_required_elements: bool = False) -> Dict[str, Any]:
    """
    Read and return the merged configuration from ENV and the database.

    The result is cached in-process until a write invalidates it, so this is
    cheap enough to call from hot paths.
    """
    global _config_cache

    if _config_cache is not None and not check_required_elements:
        return _config_cache

    config_logger.debug("🔧 Loading fresh configuration...")
    
    # Load ENV config
    env_config = load_env_config()
    
    # Load DB config
    db_data = await load_db_config()
    
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
        try:
            config["limits"]["general"] = int(db_config["general_limit"])
        except (ValueError, TypeError):
            pass
    
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
            config["group_filter"]["group_ids"] = [
                int(x.strip()) for x in group_ids_str.split(",") if x.strip()
            ]
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
        config["admin_filter"]["admin_usernames"] = [
            x.strip() for x in admin_usernames_str.split(",") if x.strip()
        ]
    
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
        config["cdn_inbounds"] = [
            x.strip() for x in cdn_inbounds_str.split(",") if x.strip()
        ]
    
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
            config["cdn_nodes"] = [
                int(x.strip()) for x in cdn_nodes_str.split(",") if x.strip()
            ]
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
    config["device_count_mode"] = str(
        db_config.get("device_count_mode", "device")
    ).strip().lower()
    if config["device_count_mode"] not in ("device", "ip"):
        config["device_count_mode"] = "device"

    # Disabled nodes - list of node IDs to exclude from monitoring
    # Connections from these nodes are completely ignored
    config["disabled_nodes"] = []
    disabled_nodes_str = db_config.get("disabled_nodes", "")
    if disabled_nodes_str:
        try:
            config["disabled_nodes"] = [
                int(x.strip()) for x in disabled_nodes_str.split(",") if x.strip()
            ]
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

    # Candidate selection strategy:
    #   "online"         -> only users the panel reports as recently online
    #   "all_monitored"  -> every monitored user (heavier, no freshness window)
    config["api_ip_candidate_mode"] = str(
        db_config.get("api_ip_candidate_mode", "online")
    ).strip().lower()
    if config["api_ip_candidate_mode"] not in ("online", "all_monitored"):
        config["api_ip_candidate_mode"] = "online"

    # Online freshness window in seconds. 0 = auto (check_interval + 30s).
    try:
        config["api_ip_online_window"] = int(db_config.get("api_ip_online_window", "0"))
    except (ValueError, TypeError):
        config["api_ip_online_window"] = 0
    if config["api_ip_online_window"] < 0:
        config["api_ip_online_window"] = 0

    # Max age of a reported IP, in seconds. The panel's online-stats map keeps an
    # IP with its last-seen timestamp long after the client left, so anything
    # older than this is not a currently connected device.
    # 0 = auto (check_interval), which matches log mode's sample width.
    try:
        config["api_ip_freshness"] = int(db_config.get("api_ip_freshness", "0"))
    except (ValueError, TypeError):
        config["api_ip_freshness"] = 0
    if config["api_ip_freshness"] < 0:
        config["api_ip_freshness"] = 0

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

    # Placeholder inbound protocol name: the panel API returns {ip: count}
    # per node without any inbound information, so a sentinel is used to
    # keep the (ip, inbound) device-counting key shape intact.
    config["api_ip_sentinel_inbound"] = str(
        db_config.get("api_ip_sentinel_inbound", "API")
    ).strip() or "API"

    # Minimum successful-fetch ratio required to run enforcement for a cycle.
    # Below this the cycle is skipped entirely so that a flaky panel cannot
    # mass-reset the consecutive-violation counters of real offenders.
    try:
        config["api_ip_min_coverage"] = float(db_config.get("api_ip_min_coverage", "0.8"))
    except (ValueError, TypeError):
        config["api_ip_min_coverage"] = 0.8
    config["api_ip_min_coverage"] = max(0.0, min(1.0, config["api_ip_min_coverage"]))

    # Fraction of the expected node fleet that must appear in the API payloads.
    # ip_source_api reads this key, but nothing ever populated it, so the gate was
    # permanently off: the "only 16 of 49 nodes reported" cycles passed as healthy.
    # Default stays 0.0 (off) so wiring it up cannot change behaviour on its own.
    try:
        config["api_ip_min_node_coverage"] = float(
            db_config.get("api_ip_min_node_coverage", "0.0")
        )
    except (ValueError, TypeError):
        config["api_ip_min_node_coverage"] = 0.0
    config["api_ip_min_node_coverage"] = max(
        0.0, min(1.0, config["api_ip_min_node_coverage"])
    )

    # Automatically fall back to log mode after repeated total failures
    # (e.g. the panel account is missing the nodes:stats permission).
    config["api_ip_auto_fallback"] = db_config.get(
        "api_ip_auto_fallback", "true"
    ).lower() == "true"
    
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
        return config

    _config_cache = config
    return config


async def save_config_value(key: str, value: Any) -> bool:
    """
    Save a dynamic configuration value to database.
    
    Args:
        key: Configuration key
        value: Value to save
        
    Returns:
        True if successful
    """
    if not DB_AVAILABLE:
        return False
    
    try:
        async with get_db() as session:
            await ConfigCRUD.set(session, key, str(value))
        await invalidate_config_cache()
        return True
    except Exception:
        return False


async def delete_config_value(key: str) -> bool:
    """Delete a configuration value from database."""
    if not DB_AVAILABLE:
        return False
    
    try:
        async with get_db() as session:
            await ConfigCRUD.delete(session, key)
        await invalidate_config_cache()
        return True
    except Exception:
        return False


async def get_config_value_from_db(key: str, default: Any = None) -> Any:
    """Get a single config value from database."""
    if not DB_AVAILABLE:
        return default
    
    try:
        async with get_db() as session:
            value = await ConfigCRUD.get(session, key, default)
            return value
    except Exception:
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
