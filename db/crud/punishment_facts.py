"""Persistence and distinct-user queries for immutable action evidence."""

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PunishmentFact


class PunishmentFactsCRUD:
    """Prepared intents are mutable only until their final completion snapshot."""

    @staticmethod
    async def store(db: AsyncSession, fact: dict) -> None:
        await db.execute(insert(PunishmentFact).values(**fact).on_conflict_do_nothing(index_elements=["incident_id"]))
        row = await db.get(PunishmentFact, fact["incident_id"], populate_existing=True)
        identity = ("username", "step_index", "started_at")
        if any(getattr(row, key) != fact[key] for key in identity):
            raise ValueError("Punishment fact identity is immutable")
        if row.state == "completed":
            if fact["state"] != "completed":
                # Replay of an old prepared snapshot cannot regress completion.
                return
            if any(getattr(row, key) != value for key, value in fact.items()):
                raise ValueError("Completed punishment fact is immutable")
            return
        for key, value in fact.items():
            setattr(row, key, value)
        await db.flush()

    @staticmethod
    async def matches(db: AsyncSession, fact: dict) -> bool:
        row = await db.get(PunishmentFact, fact["incident_id"])
        return row is not None and all(getattr(row, key) == value for key, value in fact.items())

    @staticmethod
    async def unresolved_before(db: AsyncSession, end: float) -> bool:
        result = await db.scalar(
            select(PunishmentFact.incident_id)
            .where(PunishmentFact.started_at < end, PunishmentFact.state != "completed")
            .limit(1)
        )
        return result is not None

    @staticmethod
    async def aggregate(db: AsyncSession, start: float, end: float) -> tuple[dict[int | None, int], int]:
        filters = (
            PunishmentFact.state == "completed",
            PunishmentFact.disable_succeeded.is_(True),
            PunishmentFact.applied_at >= start,
            PunishmentFact.applied_at < end,
        )
        rows = await db.execute(
            select(PunishmentFact.step_index, func.count(func.distinct(PunishmentFact.username)))
            .where(*filters)
            .group_by(PunishmentFact.step_index)
        )
        total = await db.scalar(select(func.count(func.distinct(PunishmentFact.username))).where(*filters))
        return dict(rows.all()), total or 0
