"""Separate conditional ownership for reporting terminal customer outcomes."""

from sqlalchemy import select, update

from db.models import MirzaNotification

TERMINAL = ("delivered", "failed", "skipped", "uncertain")


class MirzaReportsCRUD:
    @staticmethod
    async def due(db, limit=25):
        return list(
            (
                await db.scalars(
                    select(MirzaNotification)
                    .where(MirzaNotification.report_state == "pending", MirzaNotification.state.in_(TERMINAL))
                    .order_by(MirzaNotification.applied_at, MirzaNotification.incident_id)
                    .limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def recover(db):
        # A previous process may have sent its claimed report without a receipt.
        await db.execute(
            update(MirzaNotification)
            .where(MirzaNotification.report_state == "sending")
            .values(report_state="uncertain")
        )

    @staticmethod
    async def claim(db, incident_id, chat_id, thread_id):
        result = await db.execute(
            update(MirzaNotification)
            .where(
                MirzaNotification.incident_id == incident_id,
                MirzaNotification.report_state == "pending",
                MirzaNotification.state.in_(TERMINAL),
            )
            .values(report_state="sending", report_chat_id=chat_id, report_thread_id=thread_id)
        )
        return result.rowcount == 1

    @staticmethod
    async def finish(db, incident_id, state, message_id=None):
        if state not in ("delivered", "failed", "uncertain"):
            raise ValueError("Invalid notification report state")
        result = await db.execute(
            update(MirzaNotification)
            .where(
                MirzaNotification.incident_id == incident_id,
                MirzaNotification.report_state == "sending",
            )
            .values(report_state=state, report_message_id=message_id)
        )
        return result.rowcount == 1
