"""
Shared in-memory state and synchronization primitives across PG-Limiter modules.
Decouples state ownership to eliminate circular dependencies between check_usage and parse_logs.
"""

import asyncio
from utils.types import UserType

# Global state of currently active connected users
ACTIVE_USERS: dict[str, UserType] = {}

# Module-level lock protecting concurrent access to ACTIVE_USERS
ACTIVE_USERS_LOCK = asyncio.Lock()


async def get_active_users_snapshot() -> dict[str, UserType]:
    """
    Take an atomic point-in-time snapshot of ACTIVE_USERS with cloned IP lists.
    Guarantees callers have an isolated, thread-safe view without blocking ongoing log streaming.
    """
    async with ACTIVE_USERS_LOCK:
        snapshot = {}
        for email, user in ACTIVE_USERS.items():
            if not email or not user:
                continue
            snapshot[email] = UserType(
                name=user.name,
                status=user.status,
                ip=list(user.ip) if hasattr(user, "ip") and user.ip else [],
                isp_info=user.isp_info,
                device_info=user.device_info,
                panel_status=user.panel_status,
                data_limit=user.data_limit,
                used_traffic=user.used_traffic,
                lifetime_used_traffic=user.lifetime_used_traffic,
                expire=user.expire,
                group_ids=user.group_ids,
                online_at=user.online_at,
                admin_username=user.admin_username,
                is_monitored=getattr(user, "is_monitored", True),
                effective_ip_limit=getattr(user, "effective_ip_limit", None),
            )
        return snapshot
