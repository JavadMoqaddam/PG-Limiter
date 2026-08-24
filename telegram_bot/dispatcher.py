"""
Telegram Message Dispatcher Module for PG-Limiter.

Provides a robust, centralized message dispatching engine with:
1. Token Bucket Rate Limiting (optimized for Telegram Supergroup & Forum Topic limits)
2. Tuple-Safe PriorityQueue (Priority 1: Disable/Enable, Priority 2: Warnings, Priority 3: System)
3. Database Persistence for Priority 1 messages (zero data loss across restarts)
4. Dual Cancel-or-Delete workflow for user disable messages
5. Global backoff handling on HTTP 429 RetryAfter
6. Unified throttling for send_message, edit_message, and delete_message
7. Graceful shutdown with in-flight task draining
"""

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
import html
import itertools
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from telegram.error import (
        BadRequest,
        ChatNotFound,
        Forbidden,
        NetworkError,
        RetryAfter,
        TimedOut,
    )
except ImportError:
    class TelegramError(Exception):
        """Fallback base exception when telegram package is not installed."""
        pass

    class BadRequest(TelegramError):
        pass

    class ChatNotFound(TelegramError):
        pass

    class Forbidden(TelegramError):
        pass

    class NetworkError(TelegramError):
        pass

    class RetryAfter(TelegramError):
        def __init__(self, retry_after: float = 0.0, *args):
            super().__init__(*args)
            self.retry_after = retry_after

    class TimedOut(TelegramError):
        pass

from telegram_bot.topics import TopicType, get_topics_manager
from utils.atomic_io import atomic_write_json
from utils.logs import get_logger

dispatcher_logger = get_logger("telegram.dispatcher")

# Persistence file for Priority 1 (Critical) messages
PENDING_NOTIFICATIONS_FILE = "data/pending_notifications.json"


class Priority(IntEnum):
    """Priority levels for the Telegram Dispatcher."""
    CRITICAL = 1  # Disable/Enable notifications, user bans/unbans, admin manual actions (No TTL)
    WARNINGS = 2  # Chunked warning reports from scan cycles (Dynamic TTL = check_interval)
    SYSTEM = 3    # Node status updates in General topic, backup notifications, general logs


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
    is_cancelled: bool = False
    future: Optional[asyncio.Future] = None
    retry_count: int = 0
    max_retries: int = 3
    db_id: Optional[str] = None


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
    """
    Centralized dispatcher managing all outgoing Telegram API calls with rate-limiting,
    prioritization, deduplication, and resilience against network / topic failures.
    """

    def __init__(self):
        self.bucket = TokenBucket(rate=0.8, capacity=20.0)
        # PriorityQueue stores tuples of (priority, sequence_id, QueueItem)
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sequence_counter = itertools.count()
        self._active_cancel_keys: Dict[str, QueueItem] = {}
        self._paused_until: float = 0.0
        self._is_running: bool = False
        self._worker_task: Optional[asyncio.Task] = None
        self._persistence_lock = asyncio.Lock()
        self._pending_storage: Dict[str, dict] = {}
        self._bot = None
        self._load_pending_storage()

    def set_bot(self, bot) -> None:
        """Inject bot instance directly to decouple dispatcher from application import."""
        self._bot = bot

    def _load_pending_storage(self) -> None:
        """Load pending notifications from persistent JSON storage."""
        try:
            if os.path.exists(PENDING_NOTIFICATIONS_FILE):
                with open(PENDING_NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
                    self._pending_storage = json.load(f)
                dispatcher_logger.debug(f"📁 Loaded {len(self._pending_storage)} pending notifications from storage")
        except Exception as e:
            dispatcher_logger.error(f"❌ Failed to load pending notifications: {e}")
            self._pending_storage = {}

    def _sync_save_pending_storage(self) -> None:
        """Synchronously persist pending notifications to disk."""
        try:
            atomic_write_json(PENDING_NOTIFICATIONS_FILE, self._pending_storage)
        except Exception as e:
            dispatcher_logger.error(f"❌ Failed to save pending notifications: {e}")

    async def _save_pending_storage(self) -> None:
        """Save pending storage asynchronously."""
        async with self._persistence_lock:
            await asyncio.to_thread(self._sync_save_pending_storage)

    async def reload_pending_critical_messages(self) -> None:
        """Reload any un-sent Priority 1 messages on startup and re-enqueue them."""
        if not self._pending_storage:
            return

        dispatcher_logger.info(f"🔄 Re-enqueuing {len(self._pending_storage)} pending critical notifications...")
        for db_id, data in list(self._pending_storage.items()):
            topic_str = data.get("topic_type", "disable_enable")
            try:
                topic_enum = TopicType(topic_str)
            except ValueError:
                topic_enum = TopicType.DISABLE_ENABLE

            cancel_key = data.get("cancel_key")
            reply_markup = None
            if cancel_key and cancel_key.startswith("disable:"):
                username_part = cancel_key.split("disable:", 1)[1]
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    reply_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"✅ Enable {username_part}", callback_data=f"enable_user:{username_part}")]
                    ])
                except ImportError:
                    pass

            item = QueueItem(
                priority=Priority.CRITICAL,
                action_type=data.get("action_type", ActionType.SEND),
                chat_id=data.get("chat_id"),
                text=data.get("text"),
                topic_type=topic_enum,
                reply_markup=reply_markup,
                message_thread_id=data.get("message_thread_id"),
                message_id=data.get("message_id"),
                created_at=data.get("created_at", time.time()),
                cancel_key=cancel_key,
                db_id=db_id,
            )
            if cancel_key:
                self._active_cancel_keys[cancel_key] = item

            seq = next(self._sequence_counter)
            await self.queue.put((item.priority, seq, item))

    async def enqueue_send(
        self,
        text: str,
        topic_type: TopicType = TopicType.GENERAL,
        priority: int = Priority.SYSTEM,
        reply_markup: Any = None,
        ttl: Optional[float] = None,
        cancel_key: Optional[str] = None,
        return_future: bool = False,
    ) -> Optional[asyncio.Future]:
        """
        Enqueue a message to be sent to a specific topic.
        
        Args:
            text: HTML formatted message text
            topic_type: Target topic enum
            priority: Priority level (Priority.CRITICAL, WARNINGS, SYSTEM)
            reply_markup: Optional inline keyboard markup
            ttl: Time-to-live in seconds (expires if not sent in time)
            cancel_key: Key used to cancel message before delivery
            return_future: If True, returns an asyncio.Future resolving to (message_id, chat_id)
            
        Returns:
            asyncio.Future or None
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future() if return_future else None

        db_id = None
        if priority == Priority.CRITICAL:
            db_id = f"{int(time.time() * 1000)}_{next(self._sequence_counter)}"
            self._pending_storage[db_id] = {
                "action_type": ActionType.SEND,
                "text": text,
                "topic_type": topic_type.value,
                "cancel_key": cancel_key,
                "created_at": time.time(),
                "status": "pending",
            }
            await self._save_pending_storage()

        item = QueueItem(
            priority=priority,
            action_type=ActionType.SEND,
            text=text,
            topic_type=topic_type,
            reply_markup=reply_markup,
            ttl=ttl,
            cancel_key=cancel_key,
            future=future,
            db_id=db_id,
        )

        if cancel_key:
            self._active_cancel_keys[cancel_key] = item

        seq = next(self._sequence_counter)
        await self.queue.put((priority, seq, item))
        dispatcher_logger.debug(f"📥 Enqueued message (priority={priority}, topic={topic_type.value}, cancel_key={cancel_key})")
        return future

    async def enqueue_edit(
        self,
        chat_id: int,
        message_id: int,
        new_text: str,
        priority: int = Priority.SYSTEM,
        return_future: bool = False,
    ) -> Optional[asyncio.Future]:
        """Enqueue a message edit action."""
        loop = asyncio.get_running_loop()
        future = loop.create_future() if return_future else None

        item = QueueItem(
            priority=priority,
            action_type=ActionType.EDIT,
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            future=future,
        )
        seq = next(self._sequence_counter)
        await self.queue.put((priority, seq, item))
        dispatcher_logger.debug(f"📥 Enqueued message edit (chat={chat_id}, msg={message_id})")
        return future

    async def enqueue_delete(
        self,
        chat_id: int,
        message_id: int,
        priority: int = Priority.CRITICAL,
        return_future: bool = False,
    ) -> Optional[asyncio.Future]:
        """Enqueue a message delete action."""
        loop = asyncio.get_running_loop()
        future = loop.create_future() if return_future else None

        item = QueueItem(
            priority=priority,
            action_type=ActionType.DELETE,
            chat_id=chat_id,
            message_id=message_id,
            future=future,
        )
        seq = next(self._sequence_counter)
        await self.queue.put((priority, seq, item))
        dispatcher_logger.debug(f"📥 Enqueued message delete (chat={chat_id}, msg={message_id})")
        return future

    def cancel_pending(self, cancel_key: str) -> bool:
        """
        Cancel a pending message in the queue if it has not yet been dispatched.
        
        Args:
            cancel_key: The cancel key associated with the message
            
        Returns:
            True if a pending message was found and marked cancelled, False otherwise
        """
        if cancel_key in self._active_cancel_keys:
            item = self._active_cancel_keys.pop(cancel_key)
            item.is_cancelled = True
            if item.db_id and item.db_id in self._pending_storage:
                del self._pending_storage[item.db_id]
                asyncio.create_task(self._save_pending_storage())
            if item.future and not item.future.done():
                item.future.set_result(None)
            dispatcher_logger.info(f"🚫 Cancelled pending message before send: {cancel_key}")
            return True
        return False

    async def start_worker(self) -> None:
        """Start the background dispatcher worker loop."""
        if self._is_running:
            return
        self._is_running = True
        self._worker_task = asyncio.current_task()
        dispatcher_logger.info("🚀 Telegram Dispatcher worker started")
        await self.reload_pending_critical_messages()

        try:
            while self._is_running:
                try:
                    priority, seq, item = await self.queue.get()
                except asyncio.CancelledError:
                    break

                # Check if item was cancelled before sending
                if item.is_cancelled:
                    if item.cancel_key and item.cancel_key in self._active_cancel_keys:
                        self._active_cancel_keys.pop(item.cancel_key, None)
                    self.queue.task_done()
                    continue

                # Check if item expired (TTL)
                now = time.time()
                if item.ttl and (now - item.created_at) > item.ttl:
                    dispatcher_logger.debug(f"⏰ Dropped expired item (topic={item.topic_type.value}, age={now - item.created_at:.1f}s, ttl={item.ttl}s)")
                    if item.future and not item.future.done():
                        item.future.set_result(None)
                    self.queue.task_done()
                    continue

                # Enforce global pause if currently in backoff from 429
                mono_now = time.monotonic()
                if mono_now < self._paused_until:
                    sleep_duration = self._paused_until - mono_now
                    dispatcher_logger.warning(f"⏳ Dispatcher paused due to rate limit, waiting {sleep_duration:.1f}s...")
                    await asyncio.sleep(sleep_duration)

                # Consume token from TokenBucket
                await self.bucket.consume(1.0)

                # Execute action
                success = await self._execute_item(item)
                if not success and not item.is_cancelled:
                    # Critical messages (Disable/Enable) retry indefinitely; non-critical retry up to max_retries
                    if item.priority == Priority.CRITICAL or item.retry_count < item.max_retries:
                        item.retry_count += 1
                        backoff = min(2.0 ** min(item.retry_count, 6), 30.0)
                        dispatcher_logger.warning(f"⚠️ Retrying item (attempt {item.retry_count}) in {backoff:.1f}s...")
                        await asyncio.sleep(backoff)
                        new_seq = next(self._sequence_counter)
                        await self.queue.put((item.priority, new_seq, item))
                    else:
                        if item.cancel_key and item.cancel_key in self._active_cancel_keys:
                            self._active_cancel_keys.pop(item.cancel_key, None)
                else:
                    # Finalized successfully or cancelled
                    if item.cancel_key and item.cancel_key in self._active_cancel_keys:
                        self._active_cancel_keys.pop(item.cancel_key, None)
                    if item.db_id and item.db_id in self._pending_storage:
                        del self._pending_storage[item.db_id]
                        await self._save_pending_storage()

                self.queue.task_done()

        except asyncio.CancelledError:
            dispatcher_logger.info("🛑 Telegram Dispatcher worker cancelled")
        except Exception as e:
            dispatcher_logger.error(f"❌ Unexpected error in Telegram Dispatcher worker: {e}", exc_info=True)
        finally:
            self._is_running = False

    async def _execute_item(self, item: QueueItem) -> bool:
        """Execute a single queue item via the Telegram Bot API."""
        bot = self._bot
        if not bot:
            try:
                from telegram_bot.main import application
                if application and application.bot:
                    bot = application.bot
            except Exception:
                pass

        if not bot:
            dispatcher_logger.warning("⚠ Telegram application or bot instance not initialized")
            return False

        topics_manager = get_topics_manager()

        try:
            if item.action_type == ActionType.SEND:
                group_id = topics_manager.group_id
                if not group_id:
                    dispatcher_logger.warning("⚠️ No forum group ID configured for Telegram topics")
                    if item.future and not item.future.done():
                        item.future.set_result(None)
                    return True

                thread_id = topics_manager.get_topic_id(item.topic_type) or topics_manager.get_topic_id(TopicType.GENERAL)
                kwargs = {
                    "chat_id": group_id,
                    "text": item.text,
                    "parse_mode": item.parse_mode,
                    "reply_markup": item.reply_markup,
                }
                if thread_id:
                    kwargs["message_thread_id"] = thread_id

                sent_msg = await bot.send_message(**kwargs)
                result_info = (sent_msg.message_id, group_id)
                dispatcher_logger.debug(f"✅ Sent message {sent_msg.message_id} to topic '{item.topic_type.value}' (thread={thread_id})")

                if item.future and not item.future.done():
                    item.future.set_result(result_info)
                return True

            elif item.action_type == ActionType.EDIT:
                await bot.edit_message_text(
                    chat_id=item.chat_id,
                    message_id=item.message_id,
                    text=item.text,
                    parse_mode=item.parse_mode,
                )
                dispatcher_logger.debug(f"✅ Edited message {item.message_id} in chat {item.chat_id}")
                if item.future and not item.future.done():
                    item.future.set_result(True)
                return True

            elif item.action_type == ActionType.DELETE:
                await bot.delete_message(
                    chat_id=item.chat_id,
                    message_id=item.message_id,
                )
                dispatcher_logger.debug(f"✅ Deleted message {item.message_id} in chat {item.chat_id}")
                if item.future and not item.future.done():
                    item.future.set_result(True)
                return True

        except RetryAfter as e:
            dispatcher_logger.warning(f"⚠️ Telegram 429 Flood Control: retry after {e.retry_after}s")
            self._paused_until = time.monotonic() + e.retry_after + 1.0
            return False

        except BadRequest as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                dispatcher_logger.debug("ℹ Message not modified (ignoring no-op)")
                if item.future and not item.future.done():
                    item.future.set_result(True)
                return True

            if "message thread not found" in err_str or "thread not found" in err_str:
                dispatcher_logger.error(f"❌ Forum topic '{item.topic_type.value}' thread ID not found in group: {e}")
                if item.future and not item.future.done():
                    item.future.set_result(None)
                return True  # Do not retry broken topic thread endlessly

            if "message to delete not found" in err_str or "message can't be deleted" in err_str:
                dispatcher_logger.debug(f"ℹ Message to delete already removed or expired: {e}")
                if item.future and not item.future.done():
                    item.future.set_result(False)
                return True

            dispatcher_logger.error(f"❌ Telegram BadRequest error: {e}")
            if item.future and not item.future.done():
                item.future.set_result(None)
            return True  # Discard permanent 400 Bad Request to prevent queue blockage (DLQ)

        except (Forbidden, ChatNotFound) as e:
            dispatcher_logger.error(f"❌ Bot lacks permission or chat not found: {e}")
            if item.future and not item.future.done():
                item.future.set_result(None)
            return True

        except (TimedOut, NetworkError, TimeoutError) as e:
            dispatcher_logger.warning(f"⚠️ Network error / timeout sending Telegram message: {e}")
            return False

        except Exception as e:
            err_name = type(e).__name__
            err_msg = str(e)
            if "timed out" in err_msg.lower() or "timeout" in err_name.lower() or "network" in err_name.lower() or "connection" in err_msg.lower():
                dispatcher_logger.warning(f"⚠️ Network error / timeout sending Telegram message: {e}")
            else:
                dispatcher_logger.error(f"❌ Unexpected error sending Telegram message: {e}", exc_info=True)
            return False

        return True

    async def stop(self, wait_seconds: float = 5.0) -> None:
        """
        Gracefully stop the Telegram Dispatcher worker and drain pending tasks.
        
        Args:
            wait_seconds: Maximum seconds to wait for in-flight tasks before stopping
        """
        dispatcher_logger.info(f"🛑 Stopping Telegram Dispatcher (draining queue, max wait: {wait_seconds}s)...")
        self._is_running = False

        if self._worker_task and not self._worker_task.done():
            try:
                # Wait briefly for queue to drain if items exist
                start_wait = time.time()
                while not self.queue.empty() and (time.time() - start_wait < wait_seconds):
                    await asyncio.sleep(0.2)
                self._worker_task.cancel()
                await asyncio.wait_for(self._worker_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                dispatcher_logger.debug(f"Worker shutdown note: {e}")

        # Ensure remaining state is persisted
        await self._save_pending_storage()
        dispatcher_logger.info("✓ Telegram Dispatcher stopped cleanly")


# Global singleton instance
_dispatcher_instance: Optional[TelegramDispatcher] = None


def get_dispatcher() -> TelegramDispatcher:
    """Get or create the global TelegramDispatcher singleton instance."""
    global _dispatcher_instance
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _dispatcher_instance is None:
        _dispatcher_instance = TelegramDispatcher()
    elif current_loop and getattr(_dispatcher_instance.queue, "_loop", None) is not current_loop:
        _dispatcher_instance = TelegramDispatcher()
    return _dispatcher_instance
