"""
Violation History CRUD operations.
"""

import time
from typing import Optional, List

from sqlalchemy import select, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ViolationHistory
from utils.logs import get_logger

db_violations_logger = get_logger("db.violations")


class ViolationHistoryCRUD:
    """CRUD operations for ViolationHistory table."""
    
    @staticmethod
    async def add(
        db: AsyncSession,
        username: str,
        step_applied: int,
        disable_duration: int,
        ip_count: Optional[int] = None,
        ips: Optional[List[str]] = None,
        timestamp: Optional[float] = None,
    ) -> ViolationHistory:
        """
        Add a violation record.

        ``timestamp`` exists for the JSON importer. It used to be hardcoded to
        ``time.time()``, which meant importing an old history stamped every record
        as happening *now* - and since the escalation step is the count of
        violations inside the window, that jumped every affected user straight to
        the harshest punishment. Live callers leave it None and get now, as before.
        """
        recorded_at = time.time() if timestamp is None else float(timestamp)
        db_violations_logger.debug(f"📝 Adding violation for {username}: step={step_applied}, duration={disable_duration}min")
        violation = ViolationHistory(
            username=username,
            timestamp=recorded_at,
            step_applied=step_applied,
            disable_duration=disable_duration,
            ip_count=ip_count,
            ips=ips,
        )
        db.add(violation)
        await db.flush()
        db_violations_logger.info(f"✅ Violation recorded for {username}: step={step_applied}")
        return violation

    @staticmethod
    async def count_all(db: AsyncSession) -> int:
        """Total rows in the table, regardless of window. Used by the importer."""
        result = await db.execute(
            select(func.count(ViolationHistory.id))  # pylint: disable=not-callable
        )
        return result.scalar() or 0
    
    @staticmethod
    async def get_user_violations(
        db: AsyncSession,
        username: str,
        window_hours: int = 72,
    ) -> List[ViolationHistory]:
        """Get violations for a user within the time window."""
        db_violations_logger.debug(f"🔍 Getting violations for {username} (last {window_hours}h)")
        cutoff = time.time() - (window_hours * 3600)
        result = await db.execute(
            select(ViolationHistory)
            .where(
                and_(
                    ViolationHistory.username == username,
                    ViolationHistory.timestamp >= cutoff,
                )
            )
            .order_by(ViolationHistory.timestamp.desc())
        )
        violations = result.scalars().all()
        db_violations_logger.debug(f"✅ Found {len(violations)} violations for {username}")
        return violations
    
    @staticmethod
    async def get_violation_count(
        db: AsyncSession,
        username: str,
        window_hours: int = 72,
    ) -> int:
        """Get count of violations for a user within the time window."""
        db_violations_logger.debug(f"🔍 Counting violations for {username} (last {window_hours}h)")
        cutoff = time.time() - (window_hours * 3600)
        result = await db.execute(
            select(func.count(ViolationHistory.id))  # pylint: disable=not-callable
            .where(
                and_(
                    ViolationHistory.username == username,
                    ViolationHistory.timestamp >= cutoff,
                )
            )
        )
        count = result.scalar() or 0
        db_violations_logger.debug(f"✅ {username} has {count} violations")
        return count
    
    @staticmethod
    async def clear_user(db: AsyncSession, username: str) -> int:
        """Clear all violations for a user."""
        db_violations_logger.debug(f"🗑️ Clearing violations for {username}")
        result = await db.execute(delete(ViolationHistory).where(ViolationHistory.username == username))
        db_violations_logger.info(f"✅ Cleared {result.rowcount} violations for {username}")
        return result.rowcount
    
    @staticmethod
    async def clear_all(db: AsyncSession) -> int:
        """Clear all violation history."""
        db_violations_logger.debug("🗑️ Clearing all violation history")
        result = await db.execute(delete(ViolationHistory))
        db_violations_logger.info(f"✅ Cleared {result.rowcount} total violations")
        return result.rowcount
    
    @staticmethod
    async def cleanup_old(db: AsyncSession, days: int = 30) -> int:
        """Remove violations older than specified days."""
        db_violations_logger.debug(f"🧹 Cleaning up violations older than {days} days")
        cutoff = time.time() - (days * 24 * 3600)
        result = await db.execute(delete(ViolationHistory).where(ViolationHistory.timestamp < cutoff))
        if result.rowcount > 0:
            db_violations_logger.info(f"✅ Cleaned up {result.rowcount} old violations")
        return result.rowcount
