"""Versioned route writes within caller-owned transactions."""

from copy import deepcopy
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from db.models import MirzaRoute, MirzaRouteAdmin


class MirzaRouteError(ValueError):
    """Safe operator-facing route error without database details or secrets."""


class MirzaRoutesCRUD:
    """Never commit; revision checks and updates are one conditional SQL write."""

    @staticmethod
    def public(row: MirzaRoute) -> dict:
        return {
            "id": row.id,
            "name": row.name,
            "admin_usernames": row.admin_usernames,
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
        return list((await db.scalars(select(MirzaRoute).order_by(MirzaRoute.name))).all())

    @staticmethod
    async def get(db: AsyncSession, route_id: str) -> MirzaRoute | None:
        return await db.get(MirzaRoute, route_id, populate_existing=True)

    @staticmethod
    async def create(db: AsyncSession, name: str) -> MirzaRoute:
        if await db.scalar(select(MirzaRoute.id).where(MirzaRoute.name == name)):
            raise MirzaRouteError("A bot with this name already exists")
        row = MirzaRoute(id=str(uuid.uuid4()), name=name, admins=[])
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            raise MirzaRouteError("A bot with this name already exists") from None
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
            members = changed.pop("admin_usernames", None)
            try:
                result = await db.execute(
                    update(MirzaRoute)
                    .where(MirzaRoute.id == route_id, MirzaRoute.revision == revision)
                    .values(**changed, revision=revision + 1)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise MirzaRouteError("Route changed; reload before editing")
                if members is not None:
                    removed = set(row.admin_usernames) - set(members)
                    added = set(members) - set(row.admin_usernames)
                    if removed:
                        await db.execute(
                            delete(MirzaRouteAdmin).where(
                                MirzaRouteAdmin.route_id == route_id,
                                MirzaRouteAdmin.admin_username.in_(removed),
                            )
                        )
                    db.add_all(MirzaRouteAdmin(admin_username=name, route_id=route_id) for name in sorted(added))
                    # The PK enforces exclusivity even if two sessions both read
                    # the owner as unassigned. A failed flush rolls back all writes.
                    await db.flush()
                await MirzaNotificationsCRUD.cancel_unsent_for_route(db, route_id, revision)
            except IntegrityError:
                raise MirzaRouteError(
                    "Bot name already exists or an admin is already assigned to another bot"
                ) from None
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
        await db.execute(delete(MirzaRouteAdmin).where(MirzaRouteAdmin.route_id == route_id))
        await MirzaNotificationsCRUD.cancel_unsent_for_route(db, route_id)

    @staticmethod
    async def read_secret(db: AsyncSession, route_id: str, revision: int) -> str | None:
        """Only the captured enabled revision can provide its credential."""
        return await db.scalar(
            select(MirzaRoute.api_key).where(
                MirzaRoute.id == route_id, MirzaRoute.revision == revision, MirzaRoute.enabled.is_(True)
            )
        )
