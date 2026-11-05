"""Deliver incident-owned notifications independently of the enforcement loop."""

import asyncio
import time

from db.crud.mirza_notifications import MirzaNotificationsCRUD as Notifications
from db.crud.mirza_routes import MirzaRoutesCRUD as Routes
from utils.logs import get_logger
from utils.mirza_client import MirzaClient, MirzaResult
from utils.mirza_settings import parse_recipient, render_message
from utils.punishment_facts import get_punishment_fact_journal, settle_owned

logger = get_logger("mirza.notifications")


class MirzaNotificationService:
    """One worker owns claims, bounded retries, and conservative crash recovery."""

    def __init__(self, panel_data, client=None, clock=None):
        self.panel_data = panel_data
        self.client = client if client is not None else MirzaClient()
        self.clock = clock or time.time
        self._lock = asyncio.Lock()
        self._recovered = False

    async def _transition(self, incident_id, expected, state, **changes) -> bool:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            claimed = await Notifications.transition(db, incident_id, expected, state, **changes)
            await db.commit()
            return claimed

    async def _recover(self) -> None:
        from db.database import AsyncSessionLocal

        # Complete all recovery batches before claiming any new HTTP operation.
        while True:
            async with AsyncSessionLocal() as db:
                rows = await Notifications.due(db, ("sending",), float("inf"), limit=25)
                for row in rows:
                    await Notifications.transition(
                        db, row.incident_id, "sending", "uncertain", last_error="send_interrupted"
                    )
                await db.commit()
            if len(rows) < 25:
                self._recovered = True
                return

    async def run_once(self) -> None:
        from db.database import AsyncSessionLocal

        async with self._lock:
            if not self._recovered:
                await settle_owned(self._recover())
            journal = get_punishment_fact_journal()
            for snapshot in list(journal.pending.values()):
                await settle_owned(journal.store(snapshot))
            async with AsyncSessionLocal() as db:
                rows = await Notifications.due(db, ("pending", "ready", "retry"), self.clock(), limit=25)
            for row in rows:
                try:
                    if row.state == "pending":
                        await self._resolve(row)
                    await self._send(row.incident_id)
                except Exception:
                    # A failed commit leaves the conditional state as its owner.
                    # In particular, sending can never re-enter the due queue.
                    logger.warning("Mirza notification processing deferred after local failure")

    async def _resolve(self, event) -> None:
        from db.database import AsyncSessionLocal
        from utils.panel_api.users import get_user_details

        try:
            details = await get_user_details(self.panel_data, event.username)
        except Exception:
            attempts = event.resolve_attempts + 1
            await self._transition(
                event.incident_id,
                "pending",
                "failed" if attempts >= 3 else "pending",
                resolve_attempts=attempts,
                next_attempt_at=self.clock() + min(5 * 2 ** (attempts - 1), 300),
                last_error="metadata_unavailable",
            )
            return
        if details is None:
            await self._transition(event.incident_id, "pending", "skipped", last_error="user_missing")
            return
        if not isinstance(details, dict):
            await self._transition(event.incident_id, "pending", "skipped", last_error="metadata_invalid")
            return
        admin = details.get("admin")
        owner = admin.get("username") if isinstance(admin, dict) else admin if isinstance(admin, str) else None
        if not owner:
            owner = details.get("created_by")
        candidates = [candidate for candidate in event.context["candidates"] if candidate["admin_username"] == owner]
        if not candidates:
            await self._transition(event.incident_id, "pending", "skipped", last_error="owner_unmatched")
            return
        recipient = parse_recipient(details.get("note"))
        if recipient is None:
            await self._transition(event.incident_id, "pending", "skipped", last_error="recipient_invalid")
            return
        candidate = candidates[0]
        async with AsyncSessionLocal() as db:
            route = await Routes.get(db, candidate["route_id"])
            if (
                route is None
                or not route.enabled
                or route.revision != candidate["revision"]
                or route.admin_username != owner
            ):
                await Notifications.transition(db, event.incident_id, "pending", "skipped", last_error="route_changed")
            else:
                try:
                    message = render_message(route.message_text, event.username, event.context.get("effective_limit"))
                except ValueError:
                    await Notifications.transition(
                        db, event.incident_id, "pending", "failed", last_error="message_invalid"
                    )
                else:
                    await Notifications.transition(
                        db,
                        event.incident_id,
                        "pending",
                        "ready",
                        route_id=route.id,
                        route_revision=route.revision,
                        recipient=recipient,
                        base_url=route.base_url,
                        message_text=message,
                        last_error=None,
                    )
            await db.commit()

    async def _send(self, incident_id: str) -> None:
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            event = await Notifications.get(db, incident_id)
            if event is None or event.state not in ("ready", "retry") or event.next_attempt_at > self.clock():
                return
            key = await Routes.read_secret(db, event.route_id, event.route_revision)
            if key is None:
                await Notifications.transition(db, incident_id, event.state, "skipped", last_error="route_changed")
                await db.commit()
                return
            attempts = event.attempts + 1
            claimed = await Notifications.transition(db, incident_id, event.state, "sending", attempts=attempts)
            await db.commit()
            if not claimed:
                return
        try:
            result = await self.client.send_message(event.base_url, key, event.recipient, event.message_text)
        except asyncio.CancelledError:
            try:
                await settle_owned(self._finish(incident_id, attempts, MirzaResult("uncertain", "send_interrupted")))
            except Exception:
                # The durable sending row still owns the uncertain request.
                # A database outage must not turn shutdown cancellation into
                # an ordinary per-event failure and restart the worker loop.
                logger.warning("Interrupted Mirza delivery remains owned by its sending record")
            raise
        except Exception:
            result = MirzaResult("uncertain", "transport_interrupted")
        finally:
            key = None
        await settle_owned(self._finish(incident_id, attempts, result))

    async def _finish(self, incident_id, attempts, result) -> None:
        state = result.status
        delay = 0
        if state == "retry":
            if attempts >= 3:
                state = "failed"
            else:
                delay = max(1, min(result.retry_after or 5 * 2 ** (attempts - 1), 300))
        await self._transition(
            incident_id,
            "sending",
            state,
            next_attempt_at=self.clock() + delay,
            last_error=None if state == "delivered" else result.reason,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("Mirza notification worker deferred after local failure")
            await asyncio.sleep(1)

    async def close(self) -> None:
        await self.client.close()
