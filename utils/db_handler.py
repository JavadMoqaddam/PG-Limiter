"""
Database-backed helpers that used to have JSON counterparts.

What remains here is the ISP subnet cache, the violation history and the config
store. The disabled-users wrapper that used to live here is gone: it only
delegated to the JSON registry, and that registry is now the ``users`` table
itself - see utils/handel_dis_users.py.
"""

from typing import Dict, List, Optional

from cachetools import LRUCache
from utils.logs import logger

# SQLite is the only store since Redis was removed, so a failure to import it is
# fatal rather than something to degrade around. This block used to set
# DB_AVAILABLE = False and log "falling back to JSON storage" - but no JSON
# fallback exists in this module any more, and `init_db` / `get_db` were never
# bound, so the promise turned into a NameError at the first call instead.
try:
    from db import (
        init_db,
        get_db,
        SubnetISPCRUD,
        ViolationHistoryCRUD,
        ConfigCRUD,
    )
    DB_AVAILABLE = True
    logger.info("Database module loaded successfully")
except ImportError as import_error:
    DB_AVAILABLE = False
    logger.critical(
        f"❌ FATAL: the database module could not be imported ({import_error}). "
        f"There is no JSON fallback: the ISP cache, violation history and config "
        f"store all require SQLite."
    )
    raise


class DBSubnetISPCache:
    """
    Database-backed ISP cache by /24 subnet.
    Caches ISP info by subnet to reduce API calls.
    """

    def __init__(self):
        self._initialized = False
        self._memory_cache: LRUCache = LRUCache(maxsize=10000)  # Subnet -> ISP info

    async def _ensure_initialized(self):
        if not self._initialized:
            try:
                import asyncio
                async with asyncio.timeout(10):  # 10 second timeout for DB init
                    await init_db()
                self._initialized = True
            except asyncio.TimeoutError:
                logger.warning("DB initialization timeout, skipping cache")
            except Exception as e:
                logger.warning(f"DB initialization failed: {e}")

    @staticmethod
    def _get_subnet(ip: str) -> str:
        """Extract /24 subnet from IP using SubnetISPCRUD canonical helper."""
        if DB_AVAILABLE:
            return SubnetISPCRUD.get_subnet_from_ip(ip)
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        return ip

    async def get_cached_isp(self, ip: str) -> Optional[Dict[str, str]]:
        """
        Get cached ISP info for an IP's subnet from RAM, then SQLite.

        Args:
            ip: IP address

        Returns:
            ISP info dict if cached, None otherwise
        """
        subnet = self._get_subnet(ip)

        # 1. Check in-memory cache first
        if subnet in self._memory_cache:
            return self._memory_cache[subnet]

        # 2. Check the SQLite subnet cache with a timeout
        import asyncio
        try:
            async with asyncio.timeout(3):  # 3 second timeout for DB query
                await self._ensure_initialized()
                async with get_db() as session:
                    cached = await SubnetISPCRUD.get_by_ip(session, ip)
                    if cached:
                        isp_info = {
                            "ip": ip,
                            "isp": cached.isp,
                            "country": cached.country or "Unknown",
                            "city": cached.city or "Unknown",
                            "region": cached.region or "Unknown",
                        }
                        self._memory_cache[subnet] = isp_info
                        return isp_info
        except asyncio.TimeoutError:
            logger.debug(f"DB query timeout in get_cached_isp for {ip}")
        except Exception as e:
            logger.debug(f"DB error in get_cached_isp: {e}")

        return None

    async def cache_isp(
        self,
        ip: str,
        isp_name: str,
        country: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Cache ISP info for an IP's subnet in RAM and SQLite.

        Args:
            ip: IP address
            isp_name: ISP name
            country: Country code
            city: City name
            region: Region name
        """
        subnet = self._get_subnet(ip)
        isp_info = {
            "ip": ip,
            "isp": isp_name,
            "country": country or "Unknown",
            "city": city or "Unknown",
            "region": region or "Unknown",
        }

        # 1. Update in-memory cache
        self._memory_cache[subnet] = isp_info

        # 2. Update the SQLite subnet cache with a timeout
        import asyncio
        try:
            async with asyncio.timeout(3):  # 3 second timeout for DB save
                await self._ensure_initialized()
                async with get_db() as session:
                    await SubnetISPCRUD.cache_isp(
                        session,
                        ip=ip,
                        isp=isp_name,
                        country=country,
                        city=city,
                        region=region,
                    )
        except asyncio.TimeoutError:
            logger.debug(f"DB save timeout in cache_isp for {ip}")
        except Exception as e:
            logger.debug(f"DB error in cache_isp: {e}")

        logger.debug(f"Cached ISP for subnet {subnet}: {isp_name}")

    async def get_all_cached_subnets(self) -> Dict[str, Dict[str, str]]:
        """Get all cached subnet ISP info"""
        await self._ensure_initialized()

        # Need to add get_all method to SubnetISPCRUD
        # For now, return memory cache
        return self._memory_cache.copy()

    def clear_memory_cache(self):
        """Clear only the in-memory cache"""
        self._memory_cache.clear()


class DBViolationHistory:
    """
    Database-backed violation history for punishment system.

    DEPRECATED and unused. utils/punishment_system.py talks to
    ViolationHistoryCRUD directly, so this is a third wrapper over the same table
    that no code path reaches. It is left in place rather than deleted because
    nothing here is broken and a release is not the moment to remove a public
    name; do not build on it, and do not add a caller. The one store of record is
    the ``violation_history`` table via ViolationHistoryCRUD.
    """

    def __init__(self):
        self._initialized = False

    async def _ensure_initialized(self):
        if not self._initialized:
            await init_db()
            self._initialized = True

    async def record_violation(
        self,
        username: str,
        step_applied: int,
        duration_minutes: int,
    ):
        """Record a new violation"""
        await self._ensure_initialized()

        async with get_db() as session:
            await ViolationHistoryCRUD.add(
                session,
                username=username,
                step_applied=step_applied,
                disable_duration=duration_minutes,
            )

        logger.info(f"Recorded violation for {username} (step {step_applied})")

    async def get_violation_count(
        self, username: str, window_hours: int = 168
    ) -> int:
        """
        Get violation count within time window.

        Args:
            username: Username to check
            window_hours: Time window in hours (default 7 days)

        Returns:
            Number of violations in window
        """
        await self._ensure_initialized()

        async with get_db() as session:
            return await ViolationHistoryCRUD.get_violation_count(
                session, username, window_hours=window_hours
            )

    async def get_user_violations(self, username: str, limit: int = 10) -> List[dict]:
        """Get recent violations for a user"""
        await self._ensure_initialized()

        async with get_db() as session:
            violations = await ViolationHistoryCRUD.get_user_violations(
                session, username, window_hours=24*365  # Get all within a year
            )
            result = []
            for v in violations[:limit]:
                result.append({
                    "username": v.username,
                    "timestamp": v.timestamp,
                    "step_applied": v.step_applied,
                    "duration_minutes": v.disable_duration,
                    "enabled_at": v.enabled_at,
                })
            return result

    async def clear_user_history(self, username: str):
        """Clear all violations for a user"""
        await self._ensure_initialized()

        async with get_db() as session:
            await ViolationHistoryCRUD.clear_user(session, username)

        logger.info(f"Cleared violation history for {username}")

    async def clear_all_history(self):
        """Clear all violation history"""
        await self._ensure_initialized()

        async with get_db() as session:
            await ViolationHistoryCRUD.clear_all(session)

        logger.info("Cleared all violation history")

    async def cleanup_old(self, window_hours: int = 168):
        """Remove violations older than window"""
        await self._ensure_initialized()

        days = window_hours // 24

        async with get_db() as session:
            await ViolationHistoryCRUD.cleanup_old(session, days=days)


class DBConfig:
    """
    Database-backed configuration storage.

    DEPRECATED and unused, like DBViolationHistory above: nothing imports
    ``get_db_config``. Do not add a caller. ``set()`` writes the row and updates this
    object's private ``_cache``, but it does not touch the process-wide cache in
    utils/read_config.py - which has no expiry - so a write through here would be
    invisible to the limiter, the bot and the API until something else invalidated
    that cache. The one supported way to change a setting is
    ``ConfigCRUD.set`` (or ``save_config_value``) followed by
    ``invalidate_config_cache()``.
    """

    def __init__(self):
        self._initialized = False
        self._cache: Dict[str, str] = {}

    async def _ensure_initialized(self):
        if not self._initialized:
            await init_db()
            await self._load_cache()
            self._initialized = True

    async def _load_cache(self):
        """Load config into memory cache"""
        async with get_db() as session:
            self._cache = await ConfigCRUD.get_all(session)

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get config value"""
        await self._ensure_initialized()
        return self._cache.get(key, default)

    async def set(self, key: str, value: str):
        """Set config value"""
        await self._ensure_initialized()

        async with get_db() as session:
            await ConfigCRUD.set(session, key, value)

        self._cache[key] = value

    async def delete(self, key: str):
        """Delete config value"""
        await self._ensure_initialized()

        async with get_db() as session:
            await ConfigCRUD.delete(session, key)

        self._cache.pop(key, None)

    async def get_all(self) -> Dict[str, str]:
        """Get all config values"""
        await self._ensure_initialized()
        return self._cache.copy()


# ============================================================================
# Singleton instances
# ============================================================================

_db_subnet_cache: Optional[DBSubnetISPCache] = None
_db_violation_history: Optional[DBViolationHistory] = None
_db_config: Optional[DBConfig] = None


def get_db_subnet_cache() -> DBSubnetISPCache:
    """Get or create the database-backed subnet ISP cache"""
    global _db_subnet_cache
    if _db_subnet_cache is None:
        _db_subnet_cache = DBSubnetISPCache()
    return _db_subnet_cache


def get_db_violation_history() -> DBViolationHistory:
    """Get or create the database-backed violation history"""
    global _db_violation_history
    if _db_violation_history is None:
        _db_violation_history = DBViolationHistory()
    return _db_violation_history


def get_db_config() -> DBConfig:
    """Get or create the database-backed config"""
    global _db_config
    if _db_config is None:
        _db_config = DBConfig()
    return _db_config
