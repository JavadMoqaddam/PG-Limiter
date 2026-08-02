"""
IP History Tracker - Tracks unique IPs per user over time periods using Redis ZSETs.
Eliminates memory leaks and JSON disk I/O spikes.
"""

import time
from typing import Dict, Set, List, Tuple
from datetime import datetime
from utils.logs import logger


class IPHistoryTracker:
    """Tracks IP history for all users using Redis ZSETs (0 JSON disk I/O, 0 RAM leaks)."""
    
    def __init__(self):
        pass

    async def record_user_ips(self, username: str, ips: Set[str]):
        """Record IPs for a user using Redis ZSET with atomic Pipeline trimming."""
        if not ips or not username:
            return
        current_time = time.time()
        cutoff_48h = current_time - (48 * 3600)
        key = f"pg_limiter:user:{username}:ip_history"
        
        try:
            from utils.redis_cache import get_cache
            cache = await get_cache()
            if cache.is_connected:
                async with cache.client.pipeline(transaction=True) as pipe:
                    mapping = {ip: current_time for ip in ips}
                    pipe.zadd(key, mapping)
                    pipe.zremrangebyscore(key, "-inf", cutoff_48h)
                    await pipe.execute()
        except Exception as e:
            logger.warning(f"Failed to record IP history in Redis for {username}: {e}")

    async def get_unique_ips_since(self, username: str, hours: int) -> Set[str]:
        """Get unique IPs seen in the last X hours from Redis ZSET."""
        cutoff_time = time.time() - (hours * 3600)
        key = f"pg_limiter:user:{username}:ip_history"
        try:
            from utils.redis_cache import get_cache
            cache = await get_cache()
            if cache.is_connected:
                ips = await cache.client.zrangebyscore(key, min=cutoff_time, max="+inf")
                return set(ips or [])
        except Exception as e:
            logger.warning(f"Error fetching IP history for {username}: {e}")
        return set()

    async def get_users_exceeding_limits(self, hours: int, config_data: dict) -> List[Tuple[str, int, int, Set[str]]]:
        """Get users who exceeded their limits in the last X hours."""
        results = []
        limits_config = config_data.get("limits", {})
        except_users = set(limits_config.get("except_users", []))
        special_limit = limits_config.get("special", {})
        general_limit = limits_config.get("general", 2)
        
        try:
            from utils.redis_cache import get_cache
            cache = await get_cache()
            if cache.is_connected:
                # Scan for all ip_history keys in Redis using SCAN (non-blocking)
                keys = []
                async for key in cache.client.scan_iter(match="pg_limiter:user:*:ip_history", count=100):
                    keys.append(key)
                cutoff_time = time.time() - (hours * 3600)
                
                for key in keys:
                    # Extract username from key: pg_limiter:user:{username}:ip_history
                    parts = key.split(":")
                    if len(parts) >= 4:
                        username = parts[2]
                        if username in except_users:
                            continue
                            
                        unique_ips = await cache.client.zrangebyscore(key, min=cutoff_time, max="+inf")
                        if not unique_ips:
                            continue
                            
                        ip_count = len(unique_ips)
                        user_limit = int(special_limit.get(username, general_limit))
                        
                        if ip_count > user_limit:
                            results.append((username, ip_count, user_limit, set(unique_ips)))
        except Exception as e:
            logger.error(f"Error checking users exceeding limits: {e}")
            
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def generate_report(self, hours: int, config_data: dict, isp_detector=None) -> str:
        """Generate a formatted report of users exceeding limits."""
        users_data = await self.get_users_exceeding_limits(hours, config_data)
        
        if not users_data:
            return f"📊 <b>{hours}H IP History Report</b>\n\n✅ No users exceeded their limits in the last {hours} hours."
        
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
            ""
        ]
        
        for username, ip_count, limit, unique_ips in users_data:
            report_lines.append(f"👤 <code>{username}</code>")
            report_lines.append(f"   📍 Unique IPs: <b>{ip_count}</b> (Limit: {limit})")
            report_lines.append(f"   ⚠️ Exceeded by: <b>{ip_count - limit}</b> IPs")
            
            if isp_info_batch:
                ip_list = []
                for ip in sorted(unique_ips):
                    if ip in isp_info_batch:
                        isp_info = isp_info_batch[ip]
                        ip_with_isp = f"{ip} ({isp_info.get('isp', 'Unknown')}, {isp_info.get('country', 'Unknown')})"
                        ip_list.append(ip_with_isp)
                    else:
                        ip_list.append(ip)
                
                if len(ip_list) <= 5:
                    for ip_str in ip_list:
                        report_lines.append(f"      • {ip_str}")
                else:
                    for ip_str in ip_list[:5]:
                        report_lines.append(f"      • {ip_str}")
                    report_lines.append(f"      • ... and {len(ip_list) - 5} more")
            else:
                ip_list = sorted(unique_ips)
                if len(ip_list) <= 5:
                    for ip in ip_list:
                        report_lines.append(f"      • {ip}")
                else:
                    for ip in ip_list[:5]:
                        report_lines.append(f"      • {ip}")
                    report_lines.append(f"      • ... and {len(ip_list) - 5} more")
            
            report_lines.append("")
        
        total_ips = sum(ip_count for _, ip_count, _, _ in users_data)
        report_lines.append("─────────────────────")
        report_lines.append(f"📈 <b>Summary:</b>")
        report_lines.append(f"   • Users: {len(users_data)}")
        report_lines.append(f"   • Total Unique IPs: {total_ips}")
        report_lines.append(f"   • Period: {hours}h")
        
        return "\n".join(report_lines)


# Global instance
ip_history_tracker = IPHistoryTracker()
