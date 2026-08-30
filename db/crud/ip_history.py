"""
IP History CRUD operations.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List

from sqlalchemy import select, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import IPHistory
from utils.logs import get_logger

db_ip_logger = get_logger("db.ip_history")


class IPHistoryCRUD:
    """CRUD operations for IPHistory table."""
    
    @staticmethod
    async def record_ip(
        db: AsyncSession,
        username: str,
        ip: str,
        node_name: Optional[str] = None,
        inbound_protocol: Optional[str] = None,
    ) -> IPHistory:
        """Record an IP for a user (update if exists, create if not)."""
        db_ip_logger.debug(f"📝 Recording IP for {username}: {ip}")
        result = await db.execute(
            select(IPHistory).where(
                and_(IPHistory.username == username, IPHistory.ip == ip)
            )
        )
        history = result.scalar_one_or_none()
        
        if history:
            history.last_seen = datetime.now(timezone.utc)
            history.connection_count += 1
            if node_name:
                history.node_name = node_name
            if inbound_protocol:
                history.inbound_protocol = inbound_protocol
            db_ip_logger.debug(f"✏️ Updated IP {ip} for {username} (count: {history.connection_count})")
        else:
            history = IPHistory(
                username=username,
                ip=ip,
                node_name=node_name,
                inbound_protocol=inbound_protocol,
            )
            db.add(history)
            db_ip_logger.debug(f"➕ New IP {ip} recorded for {username}")
        
        await db.flush()
        return history
    
    @staticmethod
    async def get_user_ips(db: AsyncSession, username: str, hours: int = 24) -> List[IPHistory]:
        """Get IPs for a user within the specified hours."""
        db_ip_logger.debug(f"🔍 Getting IPs for {username} (last {hours}h)")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(
            select(IPHistory)
            .where(
                and_(
                    IPHistory.username == username,
                    IPHistory.last_seen >= cutoff,
                )
            )
            .order_by(IPHistory.last_seen.desc())
        )
        ips = result.scalars().all()
        db_ip_logger.debug(f"✅ Found {len(ips)} IPs for {username}")
        return ips
    
    @staticmethod
    async def bulk_record(db: AsyncSession, pairs: List[tuple]) -> int:
        """
        Record many ``(username, ip)`` pairs in one native upsert per chunk.

        The per-cycle IP history used to be written with one Redis pipeline per
        user; a single statement per chunk keeps the whole cycle to a couple of
        round-trips against the local database.
        """
        if not pairs:
            return 0

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = datetime.now(timezone.utc)
        chunk_size = 500
        count = 0

        # Deduplicate so one statement never contains the same conflict target
        # twice, which SQLite rejects.
        unique_pairs = list({(u, ip) for u, ip in pairs if u and ip})

        for i in range(0, len(unique_pairs), chunk_size):
            chunk = unique_pairs[i:i + chunk_size]
            records = [
                {
                    "username": username,
                    "ip": ip,
                    "first_seen": now,
                    "last_seen": now,
                    "connection_count": 1,
                }
                for username, ip in chunk
            ]
            stmt = sqlite_insert(IPHistory).values(records)
            stmt = stmt.on_conflict_do_update(
                index_elements=["username", "ip"],
                set_={
                    "last_seen": stmt.excluded.last_seen,
                    "connection_count": IPHistory.connection_count + 1,
                },
            )
            await db.execute(stmt)
            count += len(chunk)

        await db.flush()
        db_ip_logger.debug(f"📝 Recorded {count} user/IP pairs")
        return count

    @staticmethod
    async def get_unique_ips_since(db: AsyncSession, username: str, hours: int) -> set:
        """Return the IPs a single user was seen with in the last ``hours``."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(
            select(IPHistory.ip).where(
                and_(IPHistory.username == username, IPHistory.last_seen >= cutoff)
            )
        )
        return {row[0] for row in result}

    @staticmethod
    async def get_ips_grouped_since(db: AsyncSession, hours: int) -> dict:
        """Return ``{username: {ip, ...}}`` for everyone seen in the last ``hours``."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(
            select(IPHistory.username, IPHistory.ip).where(IPHistory.last_seen >= cutoff)
        )
        grouped: dict[str, set] = {}
        for username, ip in result:
            grouped.setdefault(username, set()).add(ip)
        return grouped

    @staticmethod
    async def cleanup_older_than_hours(db: AsyncSession, hours: int = 48) -> int:
        """Remove IP history older than ``hours``; the reports never look further back."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(delete(IPHistory).where(IPHistory.last_seen < cutoff))
        if result.rowcount > 0:
            db_ip_logger.debug(f"🧹 Removed {result.rowcount} IP history rows older than {hours}h")
        return result.rowcount

    @staticmethod
    async def cleanup_old(db: AsyncSession, days: int = 7) -> int:
        """Remove IP history older than specified days."""
        db_ip_logger.debug(f"🧹 Cleaning up IP history older than {days} days")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await db.execute(delete(IPHistory).where(IPHistory.last_seen < cutoff))
        if result.rowcount > 0:
            db_ip_logger.info(f"✅ Cleaned up {result.rowcount} old IP records")
        return result.rowcount
