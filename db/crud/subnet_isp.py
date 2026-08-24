"""
Subnet ISP Cache CRUD operations.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SubnetISP
from utils.logs import get_logger

db_isp_logger = get_logger("db.subnet_isp")


class SubnetISPCRUD:
    """CRUD operations for SubnetISP cache."""
    
    @staticmethod
    def get_subnet_from_ip(ip: str) -> str:
        """Extract standard /24 subnet from IP address (e.g. 192.168.1.100 -> 192.168.1.0/24)."""
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        if ":" in ip:
            parts = ip.split(":")
            return ":".join(parts[:4]) + "::/64"
        return ip
    
    @staticmethod
    async def get_by_ip(db: AsyncSession, ip: str) -> Optional[SubnetISP]:
        """Get ISP info for an IP (looks up by subnet, read-only without lock)."""
        subnet = SubnetISPCRUD.get_subnet_from_ip(ip)
        db_isp_logger.debug(f"🔍 Looking up ISP for {ip} (subnet: {subnet})")
        
        # Look up standard subnet or legacy prefix
        parts = ip.split(".")
        legacy_subnet = ".".join(parts[:3]) if len(parts) == 4 else None
        
        if legacy_subnet:
            result = await db.execute(
                select(SubnetISP).where(
                    (SubnetISP.subnet == subnet) | (SubnetISP.subnet == legacy_subnet)
                )
            )
        else:
            result = await db.execute(select(SubnetISP).where(SubnetISP.subnet == subnet))
            
        isp = result.scalar_one_or_none()
        
        if isp:
            db_isp_logger.debug(f"✅ Cache hit for {subnet}: {isp.isp}")
        else:
            db_isp_logger.debug(f"❌ Cache miss for {subnet}")
        
        return isp
    
    @staticmethod
    async def get_by_subnet(db: AsyncSession, subnet: str) -> Optional[SubnetISP]:
        """Get ISP info by subnet directly."""
        db_isp_logger.debug(f"🔍 Looking up ISP by subnet: {subnet}")
        result = await db.execute(select(SubnetISP).where(SubnetISP.subnet == subnet))
        return result.scalar_one_or_none()
    
    @staticmethod
    async def cache_isp(
        db: AsyncSession,
        ip: str,
        isp: str,
        country: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
        asn: Optional[str] = None,
        as_name: Optional[str] = None,
    ) -> SubnetISP:
        """Cache ISP info for an IP's subnet."""
        subnet = SubnetISPCRUD.get_subnet_from_ip(ip)
        db_isp_logger.debug(f"📝 Caching ISP for {subnet}: {isp}")
        
        result = await db.execute(select(SubnetISP).where(SubnetISP.subnet == subnet))
        existing = result.scalar_one_or_none()
        
        if existing:
            db_isp_logger.debug(f"✏️ Updating ISP cache for {subnet}")
            existing.isp = isp
            existing.country = country
            existing.city = city
            existing.region = region
            existing.asn = asn
            existing.as_name = as_name
            existing.cached_at = datetime.now(timezone.utc)
            existing.hit_count += 1
            return existing
        
        db_isp_logger.debug(f"➕ New ISP cache entry for {subnet}")
        subnet_isp = SubnetISP(
            subnet=subnet,
            isp=isp,
            country=country,
            city=city,
            region=region,
            asn=asn,
            as_name=as_name,
        )
        db.add(subnet_isp)
        await db.flush()
        return subnet_isp
    
    @staticmethod
    async def get_stats(db: AsyncSession) -> dict:
        """Get ISP cache statistics."""
        db_isp_logger.debug("📊 Getting ISP cache stats")
        result = await db.execute(select(func.count(SubnetISP.id)))  # pylint: disable=not-callable
        count = result.scalar()
        
        result = await db.execute(select(func.sum(SubnetISP.hit_count)))
        total_hits = result.scalar() or 0
        
        db_isp_logger.debug(f"✅ ISP cache: {count} subnets, {total_hits} total hits")
        return {
            "cached_subnets": count,
            "total_cache_hits": total_hits,
        }
    
    @staticmethod
    async def cleanup_old(db: AsyncSession, days: int = 30) -> int:
        """Remove cache entries older than specified days."""
        db_isp_logger.debug(f"🧹 Cleaning up ISP cache older than {days} days")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await db.execute(delete(SubnetISP).where(SubnetISP.cached_at < cutoff))
        if result.rowcount > 0:
            db_isp_logger.info(f"✅ Cleaned up {result.rowcount} old ISP cache entries")
        return result.rowcount
