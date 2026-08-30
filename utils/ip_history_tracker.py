"""
IP History Tracker - tracks the unique IPs a user was seen with over time.

Backed by the ``ip_history`` SQLite table. One upsert per cycle covers every
user, which replaced a Redis ZSET per user (one pipeline each) and removed the
last reason for the project to depend on Redis at all.
"""

import time
from datetime import datetime
from typing import Dict, List, Set, Tuple

from utils.logs import logger

# History older than this is never reported, so it is pruned on write.
RETENTION_HOURS = 48

# Prune at most once per this interval; the cycle calls record_many() every
# check_interval and a delete scan per cycle would be pointless.
_PRUNE_INTERVAL_SECONDS = 3600.0


class IPHistoryTracker:
    """Tracks IP history for all users in SQLite."""

    def __init__(self):
        self._last_prune = 0.0

    async def record_many(self, users_ips: Dict[str, Set[str]]) -> int:
        """
        Record the IPs of every user of the current cycle in one statement.

        Args:
            users_ips: ``{username: {ip, ...}}`` as collected this cycle.

        Returns:
            Number of user/IP pairs written.
        """
        pairs = [(username, ip) for username, ips in users_ips.items() for ip in ips]
        if not pairs:
            return 0

        try:
            from db.crud.ip_history import IPHistoryCRUD
            from db.database import get_db

            async with get_db() as db:
                written = await IPHistoryCRUD.bulk_record(db, pairs)
                now = time.time()
                if now - self._last_prune >= _PRUNE_INTERVAL_SECONDS:
                    self._last_prune = now
                    await IPHistoryCRUD.cleanup_older_than_hours(db, RETENTION_HOURS)
            return written
        except Exception as error:
            logger.warning(f"Failed to record IP history: {error}")
            return 0

    async def record_user_ips(self, username: str, ips: Set[str]) -> None:
        """Record the IPs of a single user (prefer record_many for a whole cycle)."""
        if not ips or not username:
            return
        await self.record_many({username: set(ips)})

    async def get_unique_ips_since(self, username: str, hours: int) -> Set[str]:
        """Get the unique IPs seen for a user in the last ``hours``."""
        try:
            from db.crud.ip_history import IPHistoryCRUD
            from db.database import get_db

            async with get_db() as db:
                return await IPHistoryCRUD.get_unique_ips_since(db, username, hours)
        except Exception as error:
            logger.warning(f"Error fetching IP history for {username}: {error}")
            return set()

    async def get_users_exceeding_limits(
        self, hours: int, config_data: dict
    ) -> List[Tuple[str, int, int, Set[str]]]:
        """
        Get users whose unique-IP count over the window exceeds their limit.

        The limit comes from the same pre-computed metadata the enforcement path
        uses, so the report cannot disagree with the warning system about who is
        over their limit.
        """
        results: List[Tuple[str, int, int, Set[str]]] = []
        limits_config = config_data.get("limits", {}) or {}
        general_limit = limits_config.get("general", 2)
        except_users = set(config_data.get("except_users") or [])

        try:
            from db.crud.ip_history import IPHistoryCRUD
            from db.database import get_db
            from utils.user_sync import USER_METADATA_CACHE

            async with get_db() as db:
                grouped = await IPHistoryCRUD.get_ips_grouped_since(db, hours)

            for username, unique_ips in grouped.items():
                if username in except_users:
                    continue

                metadata = USER_METADATA_CACHE.get(username) or {}
                if metadata.get("is_excepted"):
                    continue
                if metadata.get("is_monitored") is False:
                    continue

                user_limit = metadata.get("special_limit") or metadata.get("effective_ip_limit")
                try:
                    user_limit = int(user_limit) if user_limit else int(general_limit)
                except (ValueError, TypeError):
                    user_limit = int(general_limit)

                if len(unique_ips) > user_limit:
                    results.append((username, len(unique_ips), user_limit, unique_ips))
        except Exception as error:
            logger.error(f"Error checking users exceeding limits: {error}")

        results.sort(key=lambda item: item[1], reverse=True)
        return results

    async def generate_report(self, hours: int, config_data: dict, isp_detector=None) -> str:
        """Generate a formatted report of users exceeding limits."""
        users_data = await self.get_users_exceeding_limits(hours, config_data)

        if not users_data:
            return (
                f"📊 <b>{hours}H IP History Report</b>\n\n"
                f"✅ No users exceeded their limits in the last {hours} hours."
            )

        isp_info_batch = {}
        if isp_detector:
            all_ips = set()
            for _, _, _, ips in users_data:
                all_ips.update(ips)
            if all_ips:
                isp_info_batch = await isp_detector.get_multiple_isp_info(list(all_ips))

        report_lines = [
            f"📊 <b>{hours}H IP History Report</b>",
            f"⏰ Period: Last {hours} hours",
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"🚫 <b>{len(users_data)} users exceeded limits:</b>",
            "",
        ]

        for username, ip_count, limit, unique_ips in users_data:
            report_lines.append(f"👤 <code>{username}</code>")
            report_lines.append(f"   📍 Unique IPs: <b>{ip_count}</b> (Limit: {limit})")
            report_lines.append(f"   ⚠️ Exceeded by: <b>{ip_count - limit}</b> IPs")

            ip_list = []
            for ip in sorted(unique_ips):
                info = isp_info_batch.get(ip)
                if info:
                    ip_list.append(
                        f"{ip} ({info.get('isp', 'Unknown')}, {info.get('country', 'Unknown')})"
                    )
                else:
                    ip_list.append(ip)

            for ip_str in ip_list[:5]:
                report_lines.append(f"      • {ip_str}")
            if len(ip_list) > 5:
                report_lines.append(f"      • ... and {len(ip_list) - 5} more")

            report_lines.append("")

        total_ips = sum(ip_count for _, ip_count, _, _ in users_data)
        report_lines.append("─────────────────────")
        report_lines.append("📈 <b>Summary:</b>")
        report_lines.append(f"   • Users: {len(users_data)}")
        report_lines.append(f"   • Total Unique IPs: {total_ips}")
        report_lines.append(f"   • Period: {hours}h")

        return "\n".join(report_lines)


# Global instance
ip_history_tracker = IPHistoryTracker()
