"""Versioned route writes within caller-owned transactions."""

from copy import deepcopy
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MirzaRoute


class MirzaRouteError(ValueError):
    """Safe operator-facing route error without database details or secrets."""


class MirzaRoutesCRUD:
    """Never commit; revision checks and updates are one conditional SQL write."""

    @staticmethod
    def public(row: MirzaRoute) -> dict:
        return {
            "id": row.id,
            "admin_username": row.admin_username,
            "base_url": row.base_url,
            "message_text": row.message_text,
            "enabled": row.enabled,
            "trigger_mode": row.trigger_mode,
            "selected_steps": deepcopy(row.selected_steps),
            "revision": row.revision,
            "key_configured": bool(row.api_key),
        }

    @staticmethod
    async def list(db: AsyncSession) -> list[MirzaRoute]:
        return list((await db.scalars(select(MirzaRoute).order_by(MirzaRoute.admin_username))).all())

    @staticmethod
    async def get(db: AsyncSession, route_id: str) -> MirzaRoute | None:
        return await db.get(MirzaRoute, route_id, populate_existing=True)

    @staticmethod
    async def create(db: AsyncSession, admin_username: str) -> MirzaRoute:
        if await db.scalar(select(MirzaRoute.id).where(MirzaRoute.admin_username == admin_username)):
            raise MirzaRouteError("A route for this admin already exists")
        row = MirzaRoute(id=str(uuid.uuid4()), admin_username=admin_username)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def update(
        db: AsyncSession, route_id: str, changes: dict, *, expected_revision: int | None = None
    ) -> MirzaRoute:
        row = await MirzaRoutesCRUD.get(db, route_id)
        if row is None:
            raise MirzaRouteError("Route no longer exists")
        if expected_revision is not None and row.revision != expected_revision:
            raise MirzaRouteError("Route changed; reload before editing")
        changed = {key: deepcopy(value) for key, value in changes.items() if getattr(row, key) != value}
        if changed:
            from db.crud.mirza_notifications import MirzaNotificationsCRUD

            revision = row.revision
            result = await db.execute(
                update(MirzaRoute)
                .where(MirzaRoute.id == route_id, MirzaRoute.revision == revision)
                .values(**changed, revision=revision + 1)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise MirzaRouteError("Route changed; reload before editing")
            await MirzaNotificationsCRUD.cancel_unsent_for_route(db, route_id, revision)
            row = await MirzaRoutesCRUD.get(db, route_id)
        return row

    @staticmethod
    async def delete(db: AsyncSession, route_id: str, *, expected_revision: int | None = None) -> None:
        from db.crud.mirza_notifications import MirzaNotificationsCRUD

        query = delete(MirzaRoute).where(MirzaRoute.id == route_id)
        if expected_revision is not None:
            query = query.where(MirzaRoute.revision == expected_revision)
        result = await db.execute(query)
        if expected_revision is not None and result.rowcount != 1:
            raise MirzaRouteError("Route changed or no longer exists")
        await MirzaNotificationsCRUD.cancel_unsent_for_route(db, route_id)

    @staticmethod
    async def read_secret(db: AsyncSession, route_id: str, revision: int) -> str | None:
        """Only the captured enabled revision can provide its credential."""
        return await db.scalar(
            select(MirzaRoute.api_key).where(
                MirzaRoute.id == route_id, MirzaRoute.revision == revision, MirzaRoute.enabled.is_(True)
            )
        )
