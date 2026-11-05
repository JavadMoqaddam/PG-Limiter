"""Idempotent admission and atomic state ownership for Mirza Bot notifications."""

from copy import deepcopy
import re

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MirzaNotification, PunishmentFact


STATES = frozenset({"pending", "ready", "sending", "delivered", "retry", "failed", "skipped", "uncertain"})


class MirzaNotificationsCRUD:
    """All operations remain in the fact or worker caller's transaction."""

    @staticmethod
    async def ensure_for_fact(db: AsyncSession, fact: PunishmentFact | dict) -> None:
        from utils.mirza_settings import validate_notification_context

        values = (
            fact
            if isinstance(fact, dict)
            else {
                key: getattr(fact, key)
                for key in (
                    "incident_id",
                    "username",
                    "step_index",
                    "applied_at",
                    "state",
                    "notification_context",
                    "disable_succeeded",
                    "warning_succeeded",
                )
            }
        )
        context = validate_notification_context(values.get("notification_context"))
        if (
            context is None
            or not context["candidates"]
            or values["state"] != "completed"
            or values["applied_at"] is None
            or values["step_index"] is None
            or values["step_index"] != context["step_index"]
        ):
            return
        success = (
            values.get("warning_succeeded", False) if context["step_type"] == "warning" else values["disable_succeeded"]
        )
        if not success:
            return
        await db.execute(
            insert(MirzaNotification)
            .values(
                incident_id=values["incident_id"],
                username=values["username"],
                applied_at=values["applied_at"],
                context=deepcopy(context),
                state="pending",
            )
            .on_conflict_do_nothing(index_elements=["incident_id"])
        )

    @staticmethod
    async def get(db: AsyncSession, incident_id: str) -> MirzaNotification | None:
        return await db.get(MirzaNotification, incident_id, populate_existing=True)

    @staticmethod
    async def due(
        db: AsyncSession, states: tuple[str, ...], now: float, *, limit: int = 100
    ) -> list[MirzaNotification]:
        """A future retry for one admin does not block other due events."""
        if not set(states).issubset(STATES):
            raise ValueError("Invalid notification state")
        return list(
            (
                await db.scalars(
                    select(MirzaNotification)
                    .where(MirzaNotification.state.in_(states), MirzaNotification.next_attempt_at <= now)
                    .order_by(
                        MirzaNotification.next_attempt_at, MirzaNotification.applied_at, MirzaNotification.incident_id
                    )
                    .limit(limit)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    @staticmethod
    async def transition(db: AsyncSession, incident_id: str, expected_state: str, state: str, **changes) -> bool:
        """Exactly one claimant can move an event across a given state boundary."""
        allowed = {
            "route_id",
            "route_revision",
            "recipient",
            "base_url",
            "message_text",
            "attempts",
            "resolve_attempts",
            "next_attempt_at",
            "last_error",
        }
        if expected_state not in STATES or state not in STATES or not set(changes).issubset(allowed):
            raise ValueError("Invalid notification transition")
        error = changes.get("last_error")
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error)):
            raise ValueError("Notification last_error must be a safe error code")
        result = await db.execute(
            update(MirzaNotification)
            .where(MirzaNotification.incident_id == incident_id, MirzaNotification.state == expected_state)
            .values(state=state, **deepcopy(changes))
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    @staticmethod
    async def counts(db: AsyncSession) -> dict[str, int]:
        rows = await db.execute(select(MirzaNotification.state, func.count()).group_by(MirzaNotification.state))
        return {**{state: 0 for state in sorted(STATES)}, **dict(rows.all())}

    @staticmethod
    async def cancel_unsent_for_route(db: AsyncSession, route_id: str, revision: int | None = None) -> None:
        """Cancel pinned unsent payloads; a sending request retains its owner."""
        query = update(MirzaNotification).where(
            MirzaNotification.route_id == route_id, MirzaNotification.state.in_(("pending", "ready", "retry"))
        )
        if revision is not None:
            query = query.where(MirzaNotification.route_revision == revision)
        await db.execute(
            query.values(state="skipped", last_error="route_changed").execution_options(synchronize_session=False)
        )
