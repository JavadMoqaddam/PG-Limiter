"""Own confirmed action facts without making reporting a prerequisite for punishment.

SQL is authoritative. An atomic pending outbox retains snapshots while SQL is
unavailable. Unknown interrupted intents delay reports rather than inventing
success. The panel and local disk cannot participate in one atomic transaction.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
import time
from typing import Callable
import uuid

from utils.atomic_io import atomic_write_json
from utils.logs import get_logger

logger = get_logger("punishment.facts")
OUTBOX_FILE = "data/punishment_fact_outbox.json"
_current_action: ContextVar["ActionFact | None"] = ContextVar("punishment_fact", default=None)


async def settle_owned(coroutine):
    """Finish local acknowledgement before propagating caller cancellation."""
    task = asyncio.create_task(coroutine)
    interrupted = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            interrupted = error
        except Exception:
            if interrupted is not None:
                raise interrupted
            raise
    if interrupted is not None:
        raise interrupted
    return result


@dataclass
class ActionFact:
    incident_id: str
    username: str
    step_index: int | None
    started_at: float
    applied_at: float | None = None
    disable_succeeded: bool = False
    revoke_succeeded: bool = False
    uuid_reset_succeeded: bool = False
    state: str = "prepared"
    journal: "PunishmentFactJournal" = field(default=None, repr=False)
    on_disabled: Callable | None = field(default=None, repr=False)
    known_not_disabled: bool = field(default=False, repr=False)

    def snapshot(self) -> dict:
        return {
            key: getattr(self, key)
            for key in (
                "incident_id",
                "username",
                "step_index",
                "started_at",
                "applied_at",
                "disable_succeeded",
                "revoke_succeeded",
                "uuid_reset_succeeded",
                "state",
            )
        }

    def mark_disabled(self) -> None:
        """Capture success and reconciliation ownership synchronously, before any await."""
        if not self.disable_succeeded and not self.known_not_disabled:
            self.disable_succeeded = True
            self.state = "completed"
            self.applied_at = self.journal.clock()
            if self.on_disabled is not None:
                self.on_disabled()
            self.journal.pending[self.incident_id] = self.snapshot()

    def mark_partial(self, field_name: str, succeeded: bool) -> None:
        if succeeded:
            setattr(self, field_name, True)
            if self.applied_at is None:
                self.applied_at = self.journal.clock()
            self.journal.pending[self.incident_id] = self.snapshot()

    async def save(self) -> None:
        snapshot = self.snapshot()
        self.journal.pending[self.incident_id] = snapshot
        await settle_owned(self.journal.store(snapshot))


def current_action() -> ActionFact | None:
    return _current_action.get()


class PunishmentFactJournal:
    """Retain unresolved intents and confirmed completions through retries/restarts."""

    def __init__(self, filename: str = OUTBOX_FILE, clock: Callable[[], float] = time.time):
        self.filename = filename
        self.clock = clock
        self.pending: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._load_failed = False
        try:
            if os.path.exists(filename):
                with open(filename, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                expected = set(ActionFact("", "", None, 0).snapshot())
                if not isinstance(loaded, dict):
                    raise ValueError("Fact outbox must contain an object")
                for incident_id, snapshot in loaded.items():
                    if (
                        not isinstance(snapshot, dict)
                        or set(snapshot) != expected
                        or snapshot["incident_id"] != incident_id
                        or not isinstance(snapshot["username"], str)
                        or snapshot["state"] not in {"prepared", "completed", "uncertain"}
                        or (
                            snapshot["step_index"] is not None
                            and (type(snapshot["step_index"]) is not int or snapshot["step_index"] < 0)
                        )
                        or any(
                            type(snapshot[key]) is not bool
                            for key in ("disable_succeeded", "revoke_succeeded", "uuid_reset_succeeded")
                        )
                        or any(
                            type(value) not in (int, float) or not math.isfinite(value)
                            for value in [snapshot["started_at"]]
                            + ([] if snapshot["applied_at"] is None else [snapshot["applied_at"]])
                        )
                    ):
                        raise ValueError("Invalid punishment fact outbox snapshot")
                self.pending = loaded
        except Exception as error:
            self.pending = {}
            self._load_failed = True
            logger.error("Punishment fact outbox unreadable; daily reports paused: %s", error)

    async def _store_sql(self, snapshot: dict) -> None:
        from db.database import AsyncSessionLocal, DB_AVAILABLE
        from db.crud.punishment_facts import PunishmentFactsCRUD

        if not DB_AVAILABLE:
            raise RuntimeError("Punishment fact database is unavailable")
        try:
            async with AsyncSessionLocal() as db:
                await PunishmentFactsCRUD.store(db, snapshot)
                await db.commit()
        except Exception:
            # A callback can raise after physical commit. Confirm the exact
            # snapshot in a fresh session before keeping a completion pending.
            async with AsyncSessionLocal() as confirmation:
                if await PunishmentFactsCRUD.matches(confirmation, snapshot):
                    return
            raise

    async def store(self, snapshot: dict) -> None:
        async with self._lock:
            # Synchronous markers may publish a newer completion while an older
            # SQL write is in flight. Always persist the newest owned snapshot.
            snapshot = self.pending.get(snapshot["incident_id"], snapshot)
            self.pending[snapshot["incident_id"]] = snapshot
            try:
                await self._store_sql(snapshot)
            except Exception as error:
                logger.error("Punishment evidence awaiting database persistence: %s", error)
            else:
                if self.pending.get(snapshot["incident_id"]) == snapshot:
                    self.pending.pop(snapshot["incident_id"], None)
            if self._load_failed:
                return
            try:
                await asyncio.to_thread(atomic_write_json, self.filename, self.pending)
            except Exception as error:
                logger.error("Punishment fact outbox write failed; pending evidence remains owned: %s", error)

    @asynccontextmanager
    async def action(self, username: str, step_index: int | None, on_disabled: Callable | None = None):
        fact = ActionFact(uuid.uuid4().hex, username, step_index, self.clock(), journal=self, on_disabled=on_disabled)
        self.pending[fact.incident_id] = fact.snapshot()
        try:
            await fact.save()
        except asyncio.CancelledError:
            # No panel request was entered; this intent has a known empty result.
            fact.state = "completed"
            await fact.save()
            raise
        token = _current_action.set(fact)
        normal = False
        try:
            yield fact
            normal = True
        finally:
            _current_action.reset(token)
            fact.state = "completed" if normal or fact.disable_succeeded else "uncertain"
            await fact.save()

    async def ready_before(self, end: float) -> bool:
        if self._load_failed:
            return False
        # Copy stable snapshots: a still-running action retains its prepared row,
        # so aggregation cannot race its completion.
        for snapshot in list(self.pending.values()):
            await settle_owned(self.store(snapshot))
        if any(snapshot["started_at"] < end for snapshot in self.pending.values()):
            return False
        try:
            from db.database import AsyncSessionLocal
            from db.crud.punishment_facts import PunishmentFactsCRUD

            async with AsyncSessionLocal() as db:
                return not await PunishmentFactsCRUD.unresolved_before(db, end)
        except Exception as error:
            logger.error("Cannot establish complete daily punishment evidence: %s", error)
            return False


_journal: PunishmentFactJournal | None = None


def get_punishment_fact_journal() -> PunishmentFactJournal:
    global _journal
    if _journal is None:
        _journal = PunishmentFactJournal(OUTBOX_FILE)
    return _journal
