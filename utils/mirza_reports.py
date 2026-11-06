"""Report customer delivery results without sharing their retry or counter state."""

import asyncio
from datetime import datetime, UTC
import html

from db.crud.mirza_reports import MirzaReportsCRUD as Reports
from telegram_bot.dispatcher import Priority, get_dispatcher
from telegram_bot.topics import TopicType, get_topics_manager
from utils.logs import get_logger
from utils.punishment_facts import settle_owned

logger = get_logger("mirza.reports")


def format_report(event) -> str:
    """Use persisted resolved identity and safe error codes, never raw API bodies."""
    descriptions = {
        "delivered": "✅ Delivered — Mirza Bot acknowledged the message",
        "failed": "❌ Failed — no successful API acknowledgement",
        "skipped": "⏭ Skipped — message was not sent",
        "uncertain": "❔ Uncertain — delivery could not be confirmed",
    }

    def escape(value):
        return html.escape(str(value), quote=True)

    step = event.context.get("step_index")
    step = step + 1 if type(step) is int and 0 <= step < 10 else "Unknown"
    stamp = datetime.fromtimestamp(event.applied_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "🔔 <b>Mirza Bot Notification</b>\n\n"
        f"Config: <code>{escape(event.username)}</code>\n"
        f"Bot: <b>{escape(event.bot_name or 'Unknown (not resolved)')}</b>\n"
        f"Telegram ID: <code>{escape(event.recipient or 'Unknown (not resolved)')}</code>\n"
        f"Step: <b>{step}</b>\n"
        f"Punishment: {stamp}\n"
        f"Result: {descriptions[event.state]}\n"
        f"Reason: <code>{escape(event.last_error or 'acknowledged')}</code>\n"
        f"Incident: <code>{escape(event.incident_id)}</code>"
    )


class MirzaNotificationReporter:
    """One independent worker claims reports before any Telegram append."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._recovered = False

    async def _finish(self, incident_id, state, message_id=None):
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await Reports.finish(db, incident_id, state, message_id)
            await db.commit()

    async def run_once(self):
        from db.database import AsyncSessionLocal

        async with self._lock:
            if not self._recovered:
                async with AsyncSessionLocal() as db:
                    await Reports.recover(db)
                    await db.commit()
                self._recovered = True
            manager = get_topics_manager()
            topic = TopicType.MIRZA_NOTIFICATIONS
            chat_id, thread_id = manager.group_id, manager.get_topic_id(topic)
            if not manager.enabled or not chat_id or not thread_id:
                return
            async with AsyncSessionLocal() as db:
                rows = await Reports.due(db)
            for event in rows:
                # A changed/disabled topic does not redirect already admitted work.
                if not manager.enabled or manager.group_id != chat_id or manager.get_topic_id(topic) != thread_id:
                    return
                payload = format_report(event)
                async with AsyncSessionLocal() as db:
                    claimed = await Reports.claim(db, event.incident_id, chat_id, thread_id)
                    await db.commit()
                if not claimed:
                    continue
                try:
                    handle = await get_dispatcher().enqueue_send_delivery(
                        [payload],
                        topic_type=topic,
                        priority=Priority.SYSTEM,
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        message_key=f"mirza-report:{event.incident_id}",
                    )
                    result = await handle.wait()
                    state = result.status
                    message_id = None
                    if state == "delivered" and isinstance(result.receipt, tuple) and result.receipt[0] == chat_id:
                        message_id = result.receipt[1]
                    elif state not in ("failed", "cancelled"):
                        state = "uncertain"
                    if state == "cancelled":
                        state = "failed"
                    await settle_owned(self._finish(event.incident_id, state, message_id))
                    logger.info(
                        "Mirza Bot report: incident=%s customer=%s report=%s", event.incident_id, event.state, state
                    )
                except asyncio.CancelledError:
                    try:
                        await settle_owned(self._finish(event.incident_id, "uncertain"))
                    except Exception:
                        # The sending row remains durable. Never turn shutdown
                        # into an ordinary worker failure and restart the loop.
                        logger.warning("Interrupted Mirza Bot report remains owned by its sending record")
                    raise
                except Exception:
                    # A potentially accepted report is never automatically appended again.
                    await settle_owned(self._finish(event.incident_id, "uncertain"))
                    logger.warning("Mirza Bot report outcome uncertain; customer delivery unchanged")

    async def run_forever(self):
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("Mirza Bot reporting deferred after local failure")
            await asyncio.sleep(1)
