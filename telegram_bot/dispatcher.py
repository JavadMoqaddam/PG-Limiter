"""
Telegram Message Dispatcher Module for PG-Limiter.

Provides a robust, centralized message dispatching engine with:
1. Token Bucket Rate Limiting (optimized for Telegram Supergroup & Forum Topic limits)
2. Tuple-Safe PriorityQueue (Priority 1: Disable/Enable, Priority 2: Warnings, Priority 3: System)
3. Atomic JSON persistence and owned delivery receipts for Priority 1 messages
4. Dual Cancel-or-Delete workflow for user disable messages
5. Global backoff handling on HTTP 429 RetryAfter
6. Unified throttling for send_message, edit_message, and delete_message
7. Graceful shutdown with in-flight task draining
"""

import asyncio
import copy
from dataclasses import dataclass, field
from enum import Enum, IntEnum
import html
import itertools
import json
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple


from telegram_bot.topics import TopicType, get_topics_manager
from utils.atomic_io import atomic_write_json
from utils.logs import get_logger

dispatcher_logger = get_logger("telegram.dispatcher")

# Persistence file for Priority 1 (Critical) messages
PENDING_NOTIFICATIONS_FILE = "data/pending_notifications.json"

# Queued to wake a worker parked on an empty queue so it can observe a stop().
_SHUTDOWN_SENTINEL = object()


class Priority(IntEnum):
    """Priority levels for the Telegram Dispatcher."""

    CRITICAL = 1  # Disable/Enable notifications, user bans/unbans, admin manual actions (No TTL)
    WARNINGS = 2  # Chunked warning reports from scan cycles (Dynamic TTL = check_interval)
    SYSTEM = 3  # Node status updates in General topic, backup notifications, general logs


class DeliveryState(str, Enum):
    """The locally known state of one outbound event."""

    QUEUED = "queued"
    PREPARING = "preparing"
    SENDING = "sending"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"


class ActionType(str):
    """Action types handled by the dispatcher."""

    SEND = "send"
    EDIT = "edit"
    DELETE = "delete"


@dataclass
class QueueItem:
    """Represents an item in the Telegram Dispatcher priority queue."""

    priority: int
    action_type: str
    chat_id: Optional[int] = None
    text: Optional[str] = None
    topic_type: TopicType = TopicType.GENERAL
    message_thread_id: Optional[int] = None
    message_id: Optional[int] = None
    reply_markup: Any = None
    parse_mode: str = "HTML"
    ttl: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    cancel_key: Optional[str] = None
    state: DeliveryState = DeliveryState.QUEUED
    future: Optional[asyncio.Future] = None
    retry_count: int = 0
    max_retries: int = 3
    db_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    destination_pinned: bool = False
    receipt: Any = None
    last_error: str = ""
    batch_id: Optional[str] = None
    chunk_index: int = 0
    attempt_done: asyncio.Event = field(default_factory=asyncio.Event)
    enqueued: bool = False
    monitoring_revision: Optional[int] = None
    destination_revision: Optional[int] = None

    @property
    def is_cancelled(self) -> bool:
        return self.state == DeliveryState.CANCELLED


@dataclass
class SendBatch:
    """Own a split message's reservation until every chunk has settled."""

    batch_id: str
    count: int
    namespace: Optional[tuple] = None
    created_at: float = field(default_factory=time.time)
    items: dict[int, QueueItem] = field(default_factory=dict)
    completed: bool = False
    completed_at: float = 0.0
    admitted: bool = True


class TokenBucket:
    """
    Token Bucket rate limiter to enforce Telegram supergroup / global limits.
    Default capacity: 20 tokens, refill rate: 0.8 tokens/second (~48 tokens/minute).
    Allows short bursts while maintaining a safe long-term average.
    """

    def __init__(self, rate: float = 0.8, capacity: float = 20.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> None:
        """Wait until enough tokens are available, then consume them."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                # Calculate wait time until enough tokens have refilled
                deficit = tokens - self.tokens
                wait_time = max(deficit / self.rate, 0.05)
                await asyncio.sleep(wait_time)


class TelegramDispatcher:
    """Own outbound events, their immutable destinations and delivery receipts."""

    def __init__(self):
        self.bucket = TokenBucket(rate=0.8, capacity=20.0)
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sequence_counter = itertools.count()
        self._active_cancel_keys: Dict[str, dict[str, QueueItem]] = {}
        self._events: Dict[str, QueueItem] = {}
        self._admissions: dict[asyncio.Task, SendBatch] = {}
        self._batches: Dict[str, SendBatch] = {}
        self._dedup_batches: dict[tuple, SendBatch] = {}
        self._paused_until = 0.0
        self._is_running = False
        self._shutting_down = False
        self._worker_task: Optional[asyncio.Task] = None
        self._persistence_lock = asyncio.Lock()
        self._pending_storage: Dict[str, dict] = {}
        self._queued_db_ids: set[str] = set()
        self._unpersisted_destinations: set[str] = set()
        self._bot = None
        self._load_pending_storage()

    def set_bot(self, bot) -> None:
        """Inject the Telegram bot without importing the application."""
        self._bot = bot

    def _load_pending_storage(self) -> None:
        try:
            if os.path.exists(PENDING_NOTIFICATIONS_FILE):
                with open(PENDING_NOTIFICATIONS_FILE, "r", encoding="utf-8") as handle:
                    self._pending_storage = json.load(handle)
        except Exception as error:
            dispatcher_logger.error(f"Failed to load pending notifications: {error}")
            self._pending_storage = {}

    async def _save_pending_storage(self) -> None:
        """Serialize snapshots in order, even if the caller is cancelled mid-write."""
        async with self._persistence_lock:
            admission = self._admissions.get(asyncio.current_task())
            snapshot = {}
            for event_id, record in self._pending_storage.items():
                batch = self._batches.get(record.get("batch_id"))
                # Each admission writes its own reservation; other writers may
                # only carry batches whose initial durable write has succeeded.
                if batch is None or batch.admitted or batch is admission:
                    snapshot[event_id] = copy.deepcopy(record)
            writer = asyncio.create_task(asyncio.to_thread(atomic_write_json, PENDING_NOTIFICATIONS_FILE, snapshot))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await writer
                raise
            if admission:
                # Publish before releasing the lock so the next snapshot cannot
                # overwrite a newly committed batch as still unadmitted.
                admission.admitted = True

    def _pin_destination(self, item: QueueItem) -> bool:
        if item.destination_pinned:
            return True
        manager = get_topics_manager()
        if not manager.group_id:
            return False
        item.chat_id = manager.group_id
        item.message_thread_id = manager.get_topic_id(item.topic_type) or manager.get_topic_id(TopicType.GENERAL)
        item.destination_pinned = True
        return True

    def _record(self, item: QueueItem) -> dict:
        batch = self._batches.get(item.batch_id)
        markup = item.reply_markup
        if markup is not None and hasattr(markup, "to_dict"):
            markup = markup.to_dict()
        return {
            "action_type": item.action_type,
            "text": item.text,
            "topic_type": item.topic_type.value,
            "chat_id": item.chat_id,
            "message_thread_id": item.message_thread_id,
            "monitoring_revision": item.monitoring_revision,
            "destination_revision": item.destination_revision,
            "destination_pinned": item.destination_pinned,
            "message_id": item.message_id,
            "reply_markup": markup,
            "parse_mode": item.parse_mode,
            "cancel_key": item.cancel_key,
            "created_at": item.created_at,
            "ttl": item.ttl,
            "status": "pending"
            if item.state in (DeliveryState.QUEUED, DeliveryState.PREPARING, DeliveryState.SENDING)
            else item.state.value,
            "receipt": item.receipt,
            "error": item.last_error,
            "batch_id": item.batch_id,
            "chunk_index": item.chunk_index,
            "chunk_count": batch.count if batch else 1,
            "message_key": batch.namespace[-1] if batch and batch.namespace else None,
        }

    def _register(self, item: QueueItem) -> None:
        self._events[item.event_id] = item
        if item.cancel_key:
            self._active_cancel_keys.setdefault(item.cancel_key, {})[item.event_id] = item

    def _release(self, item: QueueItem) -> None:
        self._events.pop(item.event_id, None)
        if item.cancel_key:
            owners = self._active_cancel_keys.get(item.cancel_key, {})
            owners.pop(item.event_id, None)
            if not owners:
                self._active_cancel_keys.pop(item.cancel_key, None)

    async def _queue(self, item: QueueItem) -> None:
        if item.enqueued:
            return
        self._queued_db_ids.add(item.event_id)
        item.enqueued = True
        await self.queue.put((item.priority, next(self._sequence_counter), item))

    async def reload_pending_critical_messages(self) -> None:
        """Freeze legacy destinations once; known destinations and receipts never reroute."""
        changed = False
        ready = []
        for db_id, data in list(self._pending_storage.items()):
            item = self._events.get(db_id)
            if item is None:
                try:
                    topic = TopicType(data.get("topic_type", "disable_enable"))
                except ValueError:
                    topic = TopicType.DISABLE_ENABLE
                status = data.get("status", "pending")
                state = (
                    DeliveryState(status) if status in ("delivered", "failed", "cancelled") else DeliveryState.QUEUED
                )
                batch_id = data.get("batch_id") or db_id
                namespace = None
                if data.get("message_key"):
                    namespace = (
                        data.get("chat_id"),
                        data.get("message_thread_id"),
                        topic.value,
                        data.get("monitoring_revision"),
                        data.get("destination_revision"),
                        data["message_key"],
                    )
                batch = self._batches.setdefault(batch_id, SendBatch(batch_id, data.get("chunk_count", 1), namespace))
                if namespace and state != DeliveryState.FAILED:
                    self._dedup_batches[namespace] = batch
                markup = data.get("reply_markup")
                if markup:
                    from telegram import InlineKeyboardMarkup

                    markup = InlineKeyboardMarkup.de_json(markup, self._bot)
                elif (data.get("cancel_key") or "").startswith("disable:"):
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                    username = data["cancel_key"].split(":", 1)[1]
                    markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(f"Enable {username}", callback_data=f"enable_user:{username}")]]
                    )
                item = QueueItem(
                    priority=Priority.CRITICAL,
                    action_type=data.get("action_type", ActionType.SEND),
                    chat_id=data.get("chat_id"),
                    message_thread_id=data.get("message_thread_id"),
                    monitoring_revision=data.get("monitoring_revision"),
                    destination_revision=data.get("destination_revision"),
                    destination_pinned=bool(data.get("chat_id")),
                    text=data.get("text"),
                    topic_type=topic,
                    message_id=data.get("message_id"),
                    reply_markup=markup,
                    parse_mode=data.get("parse_mode", "HTML"),
                    created_at=data.get("created_at", time.time()),
                    ttl=data.get("ttl"),
                    cancel_key=data.get("cancel_key"),
                    db_id=db_id,
                    event_id=db_id,
                    state=state,
                    receipt=tuple(data["receipt"]) if isinstance(data.get("receipt"), list) else data.get("receipt"),
                    last_error=data.get("error", ""),
                    batch_id=batch_id,
                    chunk_index=data.get("chunk_index", 0),
                    future=asyncio.get_running_loop().create_future(),
                )
                batch.items[item.chunk_index] = item
                self._register(item)
            batch = self._batches.get(item.batch_id)
            # Reserved rows share this map with durable rows during admission.
            # Only the admission owner may publish them after its write succeeds.
            if batch and not batch.admitted:
                continue
            if item.state in (DeliveryState.FAILED, DeliveryState.CANCELLED):
                # A terminal chunk remains durable until all batch members
                # settle, including after a restart between cancellations.
                await self._finish(item)
                continue
            if item.event_id in self._queued_db_ids:
                continue
            if item.action_type == ActionType.SEND and not item.destination_pinned:
                if not self._pin_destination(item):
                    continue
                self._pending_storage[db_id] = self._record(item)
                self._unpersisted_destinations.add(db_id)
                batch = self._batches.get(item.batch_id)
                if batch and batch.namespace:
                    if self._dedup_batches.get(batch.namespace) is batch:
                        self._dedup_batches.pop(batch.namespace)
                    batch.namespace = (
                        item.chat_id,
                        item.message_thread_id,
                        item.topic_type.value,
                        item.monitoring_revision,
                        item.destination_revision,
                        batch.namespace[-1],
                    )
                    self._dedup_batches[batch.namespace] = batch
                changed = True
                dispatcher_logger.info(f"Pinned legacy notification {db_id} to chat {item.chat_id}")
            ready.append(item)
        # A prior pinning write may have failed after updating memory. Always
        # persist ready rows before their first queue admission on reload.
        if changed or self._unpersisted_destinations:
            await self._save_pending_storage()
            self._unpersisted_destinations.clear()
        for item in ready:
            await self._queue(item)

    async def enqueue_send_batch(
        self,
        texts: list[str],
        topic_type: TopicType = TopicType.GENERAL,
        priority: int = Priority.SYSTEM,
        reply_markup: Any = None,
        ttl: Optional[float] = None,
        cancel_key: Optional[str] = None,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        message_key: Optional[str] = None,
    ) -> list[asyncio.Future]:
        """Reserve and persist all chunks together, reusing a live dedup batch's futures."""
        if self._shutting_down or not texts or not get_topics_manager().enabled:
            return []
        monitoring_revision = get_topics_manager().monitoring_revision
        destination_revision = get_topics_manager().destination_revision
        destination = QueueItem(
            priority,
            ActionType.SEND,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            topic_type=topic_type,
            destination_pinned=bool(chat_id),
        )
        self._pin_destination(destination)
        now = time.time()
        for key, previous in list(self._dedup_batches.items()):
            if previous.completed and now - previous.completed_at >= 86400:
                self._dedup_batches.pop(key)
                self._batches.pop(previous.batch_id, None)
        namespace = (
            (
                destination.chat_id,
                destination.message_thread_id,
                topic_type.value,
                monitoring_revision,
                destination_revision,
                message_key,
            )
            if message_key
            else None
        )
        if namespace:
            existing = self._dedup_batches.get(namespace)
            if existing and (not existing.completed or now - existing.completed_at < 86400):
                return [asyncio.shield(existing.items[index].future) for index in sorted(existing.items)]
            if existing:
                self._batches.pop(existing.batch_id, None)
                self._dedup_batches.pop(namespace, None)
        batch_id = uuid.uuid4().hex
        batch = SendBatch(batch_id, len(texts), namespace, admitted=False)
        self._batches[batch_id] = batch
        if namespace:
            self._dedup_batches[namespace] = batch
        for index, text in enumerate(texts):
            item = QueueItem(
                priority,
                ActionType.SEND,
                chat_id=destination.chat_id,
                message_thread_id=destination.message_thread_id,
                destination_pinned=destination.destination_pinned,
                monitoring_revision=monitoring_revision,
                destination_revision=destination_revision,
                text=text,
                topic_type=topic_type,
                reply_markup=reply_markup if index == len(texts) - 1 else None,
                ttl=ttl,
                cancel_key=cancel_key,
                future=asyncio.get_running_loop().create_future(),
                batch_id=batch_id,
                chunk_index=index,
            )
            if priority == Priority.CRITICAL:
                item.db_id = item.event_id
            batch.items[index] = item
            self._register(item)
            if item.db_id:
                self._pending_storage[item.db_id] = self._record(item)

        async def admit():
            try:
                if priority == Priority.CRITICAL:
                    await self._save_pending_storage()
            except BaseException:
                for item in batch.items.values():
                    self._pending_storage.pop(item.db_id, None)
                    item.state = DeliveryState.CANCELLED
                    if not item.future.done():
                        item.future.set_result(None)
                    self._release(item)
                if namespace and self._dedup_batches.get(namespace) is batch:
                    self._dedup_batches.pop(namespace, None)
                self._batches.pop(batch_id, None)
                raise
            batch.admitted = True
            for item in batch.items.values():
                if not item.is_cancelled and (item.destination_pinned or priority != Priority.CRITICAL):
                    await self._queue(item)

        # Admission belongs to the dispatcher once reserved. A caller disappearing
        # during durable I/O must not leave a persisted row with no live owner.
        admission = asyncio.create_task(admit())
        self._admissions[admission] = batch

        def admission_finished(task):
            self._admissions.pop(task, None)
            if not task.cancelled():
                task.exception()

        admission.add_done_callback(admission_finished)
        try:
            await asyncio.shield(admission)
        except asyncio.CancelledError:
            await asyncio.shield(admission)
            raise
        return [asyncio.shield(item.future) for item in batch.items.values()]

    async def enqueue_send(
        self,
        text: str,
        topic_type: TopicType = TopicType.GENERAL,
        priority: int = Priority.SYSTEM,
        reply_markup: Any = None,
        ttl: Optional[float] = None,
        cancel_key: Optional[str] = None,
        return_future: bool = False,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        message_key: Optional[str] = None,
    ) -> Optional[asyncio.Future]:
        """Backward-compatible single-message admission; delivery is always internally owned."""
        futures = await self.enqueue_send_batch(
            [text], topic_type, priority, reply_markup, ttl, cancel_key, chat_id, message_thread_id, message_key
        )
        return futures[0] if return_future and futures else None

    async def enqueue_edit(
        self, chat_id: int, message_id: int, new_text: str, priority: int = Priority.SYSTEM, return_future: bool = False
    ) -> Optional[asyncio.Future]:
        if self._shutting_down:
            return None
        item = QueueItem(
            priority,
            ActionType.EDIT,
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            future=asyncio.get_running_loop().create_future(),
        )
        self._register(item)
        await self._queue(item)
        return asyncio.shield(item.future) if return_future else None

    async def enqueue_delete(
        self, chat_id: int, message_id: int, priority: int = Priority.CRITICAL, return_future: bool = False
    ) -> Optional[asyncio.Future]:
        if self._shutting_down:
            return None
        item = QueueItem(
            priority,
            ActionType.DELETE,
            chat_id=chat_id,
            message_id=message_id,
            future=asyncio.get_running_loop().create_future(),
        )
        self._register(item)
        await self._queue(item)
        return asyncio.shield(item.future) if return_future else None

    async def _settle_batch(self, item: QueueItem) -> None:
        batch = self._batches.get(item.batch_id)
        if not batch or len(batch.items) != batch.count:
            return
        if not all(part.future.done() for part in batch.items.values()):
            return
        success = all(part.state == DeliveryState.DELIVERED for part in batch.items.values())
        if not batch.completed and success and batch.namespace:
            manager = get_topics_manager()
            if (
                manager.group_id == batch.namespace[0]
                and item.monitoring_revision is not None
                and item.destination_revision is not None
            ):
                try:
                    await manager.mark_message_sent(
                        item.topic_type,
                        batch.namespace[-1],
                        chat_id=batch.namespace[0],
                        message_thread_id=batch.namespace[1],
                        monitoring_revision=item.monitoring_revision,
                        destination_revision=item.destination_revision,
                    )
                except Exception as error:
                    dispatcher_logger.error(f"Could not mark delivered message key: {error}")
        if not batch.completed:
            batch.completed_at = time.time()
            batch.completed = True
        removed = {}
        for part in batch.items.values():
            if part.db_id and part.state != DeliveryState.FAILED:
                previous = self._pending_storage.pop(part.db_id, None)
                if previous is not None:
                    removed[part.db_id] = previous
        if removed:
            try:
                await self._save_pending_storage()
            except BaseException:
                self._pending_storage.update(removed)
                raise
        if not success or not batch.namespace:
            if batch.namespace and self._dedup_batches.get(batch.namespace) is batch:
                self._dedup_batches.pop(batch.namespace, None)
            self._batches.pop(batch.batch_id, None)

    async def _finish(self, item: QueueItem, result=None) -> None:
        if item.future and not item.future.done():
            item.future.set_result(result)
        await self._settle_batch(item)
        if not (item.db_id and item.state == DeliveryState.FAILED):
            self._release(item)

    async def cancel_pending(self, cancel_key: str) -> bool:
        """Cancel every pre-send owner; wait for already-started sends to own their receipts."""
        cancelled = False
        for item in list(self._active_cancel_keys.get(cancel_key, {}).values()):
            if item.state == DeliveryState.SENDING:
                await asyncio.shield(item.attempt_done.wait())
            if item.state == DeliveryState.DELIVERED:
                await asyncio.shield(item.future)
                continue
            item.state = DeliveryState.CANCELLED
            if item.db_id:
                self._pending_storage[item.db_id] = self._record(item)
                await self._save_pending_storage()
            await self._finish(item)
            cancelled = True
        if cancelled:
            # Settle the cancellation-safe waiter views before reporting success.
            await asyncio.sleep(0)
        return cancelled

    async def _complete_delivery(self, item: QueueItem) -> None:
        if item.db_id:
            self._pending_storage[item.db_id] = self._record(item)
            await self._save_pending_storage()
        if item.action_type == ActionType.SEND and item.cancel_key and item.cancel_key.startswith("disable:"):
            from telegram_bot.send_message import track_disable_message

            await track_disable_message(
                item.cancel_key.split(":", 1)[1], item.receipt[0], item.receipt[1], event_id=item.event_id
            )
        await self._finish(item, item.receipt)

    async def _retry(self, item: QueueItem) -> bool:
        item.retry_count += 1
        if item.priority != Priority.CRITICAL and item.retry_count > item.max_retries:
            item.state = DeliveryState.FAILED
            await self._finish(item)
            return False
        item.state = DeliveryState.QUEUED
        backoff = max(min(2.0 ** min(item.retry_count, 6), 30.0), self._paused_until - time.monotonic())
        try:
            await asyncio.sleep(backoff)
        finally:
            if not item.is_cancelled:
                await self._queue(item)
        return item.enqueued

    async def start_worker(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        self._worker_task = asyncio.current_task()
        try:
            await self.reload_pending_critical_messages()
            while self._is_running:
                try:
                    _, _, item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    await self.reload_pending_critical_messages()
                    continue
                except asyncio.CancelledError:
                    break
                try:
                    if item is _SHUTDOWN_SENTINEL:
                        break
                    item.enqueued = False
                    if item.is_cancelled:
                        await self._finish(item)
                        continue
                    if item.state == DeliveryState.DELIVERED:
                        await self._complete_delivery(item)
                        continue
                    if self._observation_obsolete(item):
                        item.state = DeliveryState.CANCELLED
                        await self._finish(item)
                        continue
                    item.state = DeliveryState.PREPARING
                    if self._paused_until > time.monotonic():
                        await asyncio.sleep(self._paused_until - time.monotonic())
                    await self.bucket.consume(1.0)
                    if self._observation_obsolete(item):
                        item.state = DeliveryState.CANCELLED
                    if item.is_cancelled:
                        await self._finish(item)
                        continue
                    if item.ttl and time.time() - item.created_at > item.ttl:
                        item.state = DeliveryState.FAILED
                        item.last_error = "Message expired before delivery"
                        if item.db_id:
                            self._pending_storage[item.db_id] = self._record(item)
                            await self._save_pending_storage()
                        await self._finish(item)
                        continue
                    item.state = DeliveryState.SENDING
                    item.attempt_done.clear()
                    try:
                        outcome = await self._execute_item(item)
                    finally:
                        item.attempt_done.set()
                    if outcome == DeliveryOutcome.DELIVERED:
                        item.state = DeliveryState.DELIVERED
                        await self._complete_delivery(item)
                    elif outcome == DeliveryOutcome.FAILED:
                        item.state = DeliveryState.FAILED
                        if item.db_id:
                            self._pending_storage[item.db_id] = self._record(item)
                            await self._save_pending_storage()
                        await self._finish(item)
                    else:
                        await self._retry(item)
                except asyncio.CancelledError:
                    if item is not _SHUTDOWN_SENTINEL and not item.is_cancelled and item.state != DeliveryState.FAILED:
                        if item.state != DeliveryState.DELIVERED:
                            item.state = DeliveryState.QUEUED
                        await self._queue(item)
                    break
                except Exception as error:
                    dispatcher_logger.error(f"Error processing outbound event: {error}", exc_info=True)
                    if item.state == DeliveryState.DELIVERED:
                        try:
                            await asyncio.sleep(2.0)
                        finally:
                            await self._queue(item)
                    else:
                        await self._retry(item)
                finally:
                    if item is not _SHUTDOWN_SENTINEL and not item.enqueued:
                        self._queued_db_ids.discard(item.event_id)
                    self.queue.task_done()
        finally:
            self._is_running = False

    def _observation_obsolete(self, item: QueueItem) -> bool:
        """Discard observations invalidated by a pause, retaining applied critical incidents."""
        if item.action_type != ActionType.SEND or item.priority == Priority.CRITICAL:
            return False
        manager = get_topics_manager()
        return not manager.enabled or item.monitoring_revision != manager.monitoring_revision

    async def _execute_item(self, item: QueueItem) -> DeliveryOutcome:
        bot = self._bot
        if bot is None:
            try:
                from telegram_bot.main import application

                bot = application.bot if application else None
            except Exception:
                pass
        if bot is None:
            return DeliveryOutcome.RETRY
        try:
            if item.action_type == ActionType.SEND:
                if not item.destination_pinned:
                    return DeliveryOutcome.RETRY
                kwargs = {
                    "chat_id": item.chat_id,
                    "text": item.text,
                    "parse_mode": item.parse_mode,
                    "reply_markup": item.reply_markup,
                }
                if item.message_thread_id is not None:
                    kwargs["message_thread_id"] = item.message_thread_id
                message = await bot.send_message(**kwargs)
                item.receipt = (message.message_id, item.chat_id)
            elif item.action_type == ActionType.EDIT:
                await bot.edit_message_text(
                    chat_id=item.chat_id, message_id=item.message_id, text=item.text, parse_mode=item.parse_mode
                )
                item.receipt = True
            elif item.action_type == ActionType.DELETE:
                await bot.delete_message(chat_id=item.chat_id, message_id=item.message_id)
                item.receipt = True
            else:
                item.last_error = f"Unknown action: {item.action_type}"
                return DeliveryOutcome.FAILED
            return DeliveryOutcome.DELIVERED
        except Exception as error:
            name = type(error).__name__
            message = str(error).lower()
            item.last_error = str(error)
            if name == "RetryAfter" or hasattr(error, "retry_after"):
                delay = getattr(error, "retry_after", 30.0)
                seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
                self._paused_until = time.monotonic() + seconds + 1.0
                return DeliveryOutcome.RETRY
            if item.action_type == ActionType.EDIT and "message is not modified" in message:
                item.receipt = True
                return DeliveryOutcome.DELIVERED
            if item.action_type == ActionType.DELETE and "message to delete not found" in message:
                item.receipt = True
                return DeliveryOutcome.DELIVERED
            invalid_payload = (
                "can't parse entities",
                "message is too long",
                "message_too_long",
                "message text is empty",
                "button_data_invalid",
            )
            if name == "BadRequest" and any(reason in message for reason in invalid_payload):
                return DeliveryOutcome.FAILED
            # Permissions, missing chats/topics and network errors may be repaired.
            # They never become a delivered receipt or discard a critical message.
            return DeliveryOutcome.RETRY

    def _resolve_remaining_futures(self) -> None:
        while not self.queue.empty():
            try:
                _, _, item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            if item is _SHUTDOWN_SENTINEL:
                continue
            self._queued_db_ids.discard(item.event_id)
            item.enqueued = False
            if item.future and not item.future.done():
                item.future.set_result(None)

    async def stop(self, wait_seconds: float = 5.0) -> None:
        self._shutting_down = True
        if self._admissions:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(asyncio.shield(task) for task in self._admissions), return_exceptions=True),
                    wait_seconds,
                )
            except asyncio.TimeoutError:
                dispatcher_logger.warning("Admission drain timed out; durable writes remain owned")
        if self._worker_task and not self._worker_task.done():
            try:
                await asyncio.wait_for(self.queue.join(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                dispatcher_logger.warning("Drain timed out; retaining durable outbound events")
            self._is_running = False
            await self.queue.put((Priority.CRITICAL, next(self._sequence_counter), _SHUTDOWN_SENTINEL))
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=wait_seconds)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                await asyncio.gather(self._worker_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        self._resolve_remaining_futures()
        await self._save_pending_storage()


# Global singleton instance
_dispatcher_instance: Optional[TelegramDispatcher] = None


def get_dispatcher() -> TelegramDispatcher:
    """
    Get or create the global TelegramDispatcher singleton instance.

    Built exactly once per process. The previous version rebuilt the instance
    whenever it was called from a different event loop, which only ever
    happened while ``limiter.py`` restarted itself in-process. The rebuild
    silently dropped everything held in memory: the queued Priority 2/3
    backlog, ``_active_cancel_keys`` (so a disable notice could no longer be
    cancelled or deleted) and every future a caller was still awaiting.
    Priority 1 messages survived it only because they are on disk before they
    are queued. A crash now ends the process instead, so there is only ever one
    loop to serve.
    """
    global _dispatcher_instance
    if _dispatcher_instance is None:
        _dispatcher_instance = TelegramDispatcher()
    return _dispatcher_instance
