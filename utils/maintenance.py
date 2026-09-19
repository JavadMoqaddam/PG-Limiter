"""Owned retention maintenance.

ViolationHistoryCRUD.cleanup_old and SubnetISPCRUD.cleanup_old existed but had no
live caller, so both tables grew without bound. This supervised loop is that owner.
"""

import asyncio

from db.database import get_db, DB_AVAILABLE
from db.crud.violations import ViolationHistoryCRUD
from db.crud.subnet_isp import SubnetISPCRUD
from utils.logs import get_logger

logger = get_logger("maintenance")

# Keep a month of history/cache and prune every six hours.
RETENTION_DAYS = 30
MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60


async def run_retention_once(days: int = RETENTION_DAYS) -> dict:
    """Prune old violation history and ISP subnet cache once. Returns removed counts."""
    if not DB_AVAILABLE:
        return {"violations": 0, "subnet_isp": 0}
    async with get_db() as db:
        violations = await ViolationHistoryCRUD.cleanup_old(db, days=days)
        subnet_isp = await SubnetISPCRUD.cleanup_old(db, days=days)
    return {"violations": violations or 0, "subnet_isp": subnet_isp or 0}


async def retention_maintenance_loop(
    interval_seconds: int = MAINTENANCE_INTERVAL_SECONDS,
    days: int = RETENTION_DAYS,
) -> None:
    """Periodic retention pass; a single failed pass is logged, not fatal."""
    while True:
        try:
            counts = await run_retention_once(days)
            logger.info(
                f"🧹 Retention pass removed {counts['violations']} violation rows and "
                f"{counts['subnet_isp']} ISP cache rows"
            )
        except Exception as error:  # pylint: disable=broad-except
            logger.error(f"❌ Retention pass failed: {error}")
        await asyncio.sleep(interval_seconds)
