"""Durable daily run identity, pinned destination, state and receipt ownership."""

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from db.models import DailyReportCheckpoint, DailyReportRun


class DailyReportsCRUD:
    """Single-daemon report ownership; completed dates never change destination/window."""

    @staticmethod
    async def get(report_date: str) -> dict | None:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = await db.get(DailyReportRun, report_date)
            return (
                {column.name: getattr(row, column.name) for column in DailyReportRun.__table__.columns} if row else None
            )

    @staticmethod
    async def prepare(snapshot: dict) -> dict:
        from db.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    insert(DailyReportRun).values(**snapshot).on_conflict_do_nothing(index_elements=["report_date"])
                )
                await db.commit()
        except Exception:
            # Preserve an already physically committed date even when a commit
            # callback raises. Never create a second dispatch identity on retry.
            persisted = await DailyReportsCRUD.get(snapshot["report_date"])
            if persisted is None:
                raise
        return await DailyReportsCRUD.get(snapshot["report_date"])

    @staticmethod
    async def latest_pending(before_date: str) -> str | None:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await db.scalar(
                select(DailyReportRun.report_date)
                .where(
                    DailyReportRun.report_date <= before_date,
                    DailyReportRun.state.in_(["waiting", "prepared", "queued", "sending"]),
                )
                .order_by(DailyReportRun.report_date.desc())
                .limit(1)
            )

    @staticmethod
    async def set_payload(report_date: str, payload: str) -> dict:
        from db.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(DailyReportRun)
                    .where(DailyReportRun.report_date == report_date, DailyReportRun.state == "waiting")
                    .values(payload=payload, state="prepared")
                )
                await db.commit()
        except Exception:
            persisted = await DailyReportsCRUD.get(report_date)
            if persisted is None or persisted["payload"] != payload:
                raise
        return await DailyReportsCRUD.get(report_date)

    @staticmethod
    async def transition(report_date: str, state: str, receipt=None) -> None:
        from db.database import AsyncSessionLocal

        fields = {"state": state}
        if receipt is not None:
            fields["message_id"] = receipt[0]
        try:
            async with AsyncSessionLocal() as db:
                row = await db.get(DailyReportRun, report_date)
                if row is None:
                    raise ValueError("Missing daily report owner")
                if row.state == "delivered":
                    return
                await db.execute(
                    update(DailyReportRun).where(DailyReportRun.report_date == report_date).values(**fields)
                )
                await db.commit()
        except Exception:
            persisted = await DailyReportsCRUD.get(report_date)
            if persisted is not None and all(persisted[key] == value for key, value in fields.items()):
                return
            raise

    @staticmethod
    async def checkpoint() -> tuple[str, bool] | None:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = await db.get(DailyReportCheckpoint, 1)
            return (row.closed_date, row.enabled) if row else None

    @staticmethod
    async def advance_checkpoint(report_date: str, enabled: bool) -> None:
        from db.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    insert(DailyReportCheckpoint)
                    .values(id=1, closed_date=report_date, enabled=enabled)
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_={"closed_date": report_date, "enabled": enabled},
                        where=DailyReportCheckpoint.closed_date <= report_date,
                    )
                )
                await db.commit()
        except Exception:
            persisted = await DailyReportsCRUD.checkpoint()
            if persisted and persisted[0] >= report_date and persisted[1] == enabled:
                return
            raise
