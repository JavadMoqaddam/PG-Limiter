"""
Send logs to Telegram Bot with topic support and rate-limited priority dispatching.
All messages are strictly sent to Forum Group topics without private chat fallback.
"""

import asyncio
import json
import os
import time
from typing import Any, Optional, Tuple

from telegram_bot.dispatcher import Priority, get_dispatcher
from telegram_bot.topics import TopicType, get_topics_manager
from utils.atomic_io import atomic_write_json
from utils.logs import get_logger

tg_send_logger = get_logger("telegram.send")

# File to track disable messages for deletion
DISABLE_MESSAGES_FILE = "data/disable_messages.json"

_disable_msg_lock = asyncio.Lock()


def _load_disable_messages() -> dict:
    """Load disable messages tracking from file."""
    try:
        if os.path.exists(DISABLE_MESSAGES_FILE):
            with open(DISABLE_MESSAGES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        tg_send_logger.error(f"Error loading disable messages: {e}")
    return {}


def _save_disable_messages(data: dict) -> None:
    """Persist receipt ownership; the worker retries tracking if this fails."""
    atomic_write_json(DISABLE_MESSAGES_FILE, data)


async def _write_disable_messages(data: dict) -> None:
    writer = asyncio.create_task(asyncio.to_thread(_save_disable_messages, data))
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        await writer
        raise


def _receipts(info: dict) -> dict:
    if "receipts" in info:
        return dict(info["receipts"])
    if "message_id" in info and "chat_id" in info:
        return {
            f"legacy:{info['chat_id']}:{info['message_id']}": {
                "message_id": info["message_id"],
                "chat_id": info["chat_id"],
            }
        }
    return {}


async def track_disable_message(username: str, message_id: int, chat_id: int, event_id: Optional[str] = None) -> None:
    """Keep all event receipts while preserving the legacy latest-message view."""
    async with _disable_msg_lock:
        data = await asyncio.to_thread(_load_disable_messages)
        receipts = _receipts(data.get(username, {}))
        receipts[event_id or f"{chat_id}:{message_id}"] = {"message_id": message_id, "chat_id": chat_id}
        data[username] = {"message_id": message_id, "chat_id": chat_id, "receipts": receipts}
        await _write_disable_messages(data)


async def get_disable_message(username: str) -> Optional[Tuple[int, int]]:
    """Return the latest receipt for backward-compatible callers."""
    data = await asyncio.to_thread(_load_disable_messages)
    info = data.get(username)
    return (info["message_id"], info["chat_id"]) if info else None


async def get_disable_messages(username: str) -> list[Tuple[int, int]]:
    """Return every known delivered notice for this user's cancellation key."""
    async with _disable_msg_lock:
        data = await asyncio.to_thread(_load_disable_messages)
        return [(receipt["message_id"], receipt["chat_id"]) for receipt in _receipts(data.get(username, {})).values()]


async def remove_disable_message_tracking(username: str, message_info: Optional[Tuple[int, int]] = None) -> None:
    """Clear only the deleted receipt when supplied; never erase a newer event."""
    async with _disable_msg_lock:
        data = await asyncio.to_thread(_load_disable_messages)
        if username not in data:
            return
        if message_info is None:
            data.pop(username)
        else:
            receipts = {
                key: value
                for key, value in _receipts(data[username]).items()
                if (value["message_id"], value["chat_id"]) != message_info
            }
            if receipts:
                latest = list(receipts.values())[-1]
                data[username] = {**latest, "receipts": receipts}
            else:
                data.pop(username)
        await _write_disable_messages(data)


import html as html_lib


def _sanitize_html_message(msg: str) -> str:
    """If message contains raw HTML document tags, wrap in code tag to prevent Telegram parse errors."""
    if not isinstance(msg, str):
        msg = str(msg)
    if "<html>" in msg.lower() or "<head>" in msg.lower() or "<body>" in msg.lower():
        clean = html_lib.escape(msg)
        return f"<code>{clean[:1000]}</code>"
    return msg


def _split_message(msg: str, max_length: int) -> list[str]:
    """
    Split a long message into chunks that fit within Telegram's character limit.
    Splits on double-newline boundaries first (paragraph breaks), then single newlines,
    to preserve HTML formatting as much as possible.
    """
    if len(msg) <= max_length:
        return [msg]

    chunks = []
    remaining = msg
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try splitting at double-newline (paragraph break) within limit
        split_pos = remaining.rfind("\n\n", 0, max_length)
        if split_pos > max_length // 4:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2 :]
            continue

        # Try splitting at single newline
        split_pos = remaining.rfind("\n", 0, max_length)
        if split_pos > max_length // 4:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1 :]
            continue

        # Hard split at max_length as last resort
        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks


async def send_logs(
    msg: str,
    return_message_id: bool = False,
    reply_markup: Any = None,
    topic_type: TopicType = TopicType.GENERAL,
    message_key: Optional[str] = None,
    priority: int = Priority.SYSTEM,
    ttl: Optional[float] = None,
    cancel_key: Optional[str] = None,
) -> Optional[Tuple[int, int]]:
    """Admit one owned outbound batch with the destination captured before any await."""
    msg = _sanitize_html_message(msg)
    manager = get_topics_manager()
    if not manager.enabled:
        tg_send_logger.debug("Topics are disabled; monitoring logs are not queued")
        return None
    chat_id = manager.group_id
    if not chat_id:
        tg_send_logger.warning("No forum group configured to send logs")
        return None
    thread_id = manager.get_topic_id(topic_type) or manager.get_topic_id(TopicType.GENERAL)
    if message_key and manager.is_message_sent(topic_type, message_key):
        return None
    futures = await get_dispatcher().enqueue_send_batch(
        _split_message(msg, 3900),
        topic_type=topic_type,
        priority=priority,
        reply_markup=reply_markup,
        ttl=ttl,
        cancel_key=cancel_key,
        chat_id=chat_id,
        message_thread_id=thread_id,
        message_key=message_key,
    )
    if return_message_id and futures:
        try:
            # Caller cancellation/timeout owns only these shields, not shared delivery.
            results = await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(future) for future in futures)), timeout=10.0
            )
            return results[-1] if results and all(result is not None for result in results) else None
        except asyncio.TimeoutError:
            tg_send_logger.warning("Timeout waiting for outbound batch delivery")
        except Exception as error:
            tg_send_logger.error(f"Error waiting for outbound delivery: {error}")
    return None


async def send_warning_log(
    msg: str, return_message_id: bool = False, reply_markup: Any = None, ttl: Optional[float] = None
) -> Optional[Tuple[int, int]]:
    """Send a warning message to the warnings topic with Priority.WARNINGS."""
    return await send_logs(
        msg, return_message_id, reply_markup, TopicType.WARNINGS, priority=Priority.WARNINGS, ttl=ttl
    )


async def send_disable_enable_log(
    msg: str, return_message_id: bool = False, reply_markup: Any = None
) -> Optional[Tuple[int, int]]:
    """Send a disable/enable message to the disable_enable topic with Priority.CRITICAL."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.DISABLE_ENABLE, priority=Priority.CRITICAL)


async def send_backup_log(
    msg: str, return_message_id: bool = False, reply_markup: Any = None
) -> Optional[Tuple[int, int]]:
    """Send a backup message to the backups topic with Priority.SYSTEM."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.BACKUPS, priority=Priority.SYSTEM)


async def send_no_limit_log(
    msg: str, return_message_id: bool = False, reply_markup: Any = None
) -> Optional[Tuple[int, int]]:
    """Send a no-limit message to the no_limit topic with Priority.SYSTEM."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.NO_LIMIT, priority=Priority.SYSTEM)


async def delete_message(message_info: Optional[Tuple[int, int]]) -> bool:
    """
    Delete a message through the rate-limited dispatcher.

    Args:
        message_info: Tuple of (message_id, chat_id)

    Returns:
        True if successfully enqueued/deleted, False otherwise
    """
    if not message_info:
        return False

    message_id, chat_id = message_info
    tg_send_logger.debug(f"🗑️ Enqueueing delete for message {message_id} in chat {chat_id}")

    dispatcher = get_dispatcher()
    future = await dispatcher.enqueue_delete(
        chat_id=chat_id,
        message_id=message_id,
        priority=Priority.CRITICAL,
        return_future=True,
    )
    if future:
        try:
            return bool(await asyncio.wait_for(asyncio.shield(future), timeout=10.0))
        except asyncio.TimeoutError:
            tg_send_logger.warning(f"⏱ Timeout waiting for delete result of message {message_id}")
            return False
        except Exception as e:
            tg_send_logger.error(f"❌ Failed to delete message: {e}")
            return False
    return True


async def edit_message(message_info: Optional[Tuple[int, int]], new_text: str) -> bool:
    """
    Edit an existing message through the rate-limited dispatcher.

    Args:
        message_info: Tuple of (message_id, chat_id)
        new_text: The new text to replace the message with

    Returns:
        True if successfully edited, False otherwise
    """
    if not message_info:
        return False

    message_id, chat_id = message_info
    tg_send_logger.debug(f"✏️ Enqueueing edit for message {message_id} in chat {chat_id}")

    dispatcher = get_dispatcher()
    future = await dispatcher.enqueue_edit(
        chat_id=chat_id,
        message_id=message_id,
        new_text=new_text,
        priority=Priority.SYSTEM,
        return_future=True,
    )
    if future:
        try:
            return bool(await asyncio.wait_for(asyncio.shield(future), timeout=10.0))
        except asyncio.TimeoutError:
            tg_send_logger.warning(f"⏱ Timeout waiting for edit result of message {message_id}")
            return False
        except Exception as e:
            tg_send_logger.error(f"❌ Failed to edit message: {e}")
            return False
    return True


async def send_disable_notification(msg: str, username: str) -> None:
    """
    Send a disable notification with an Enable button to the disable/enable topic.
    Persisted with Priority.CRITICAL; confirmed receipts are tracked for deletion.

    Args:
        msg: The message text to send
        username: The username that was disabled
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    tg_send_logger.debug(f"🚫 Enqueueing disable notification for {username}")
    keyboard = [
        [InlineKeyboardButton(f"✅ Enable {username}", callback_data=f"enable_user:{username}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    cancel_key = f"disable:{username}"
    await send_logs(
        msg,
        return_message_id=False,
        reply_markup=reply_markup,
        topic_type=TopicType.DISABLE_ENABLE,
        priority=Priority.CRITICAL,
        cancel_key=cancel_key,
    )


# Backward-compatible alias
safe_send_disable_notification = send_disable_notification


async def cancel_or_delete_disable_message(username: str) -> bool:
    """Cancel pre-send events, then delete each known delivered receipt safely."""
    cancelled = await get_dispatcher().cancel_pending(f"disable:{username}")
    receipts = await get_disable_messages(username)
    success = cancelled
    failed = False
    for receipt in receipts:
        if await delete_message(receipt):
            await remove_disable_message_tracking(username, receipt)
            success = True
        else:
            # Preserve failed deletion ownership for a later cleanup attempt.
            failed = True
    return success and not failed


async def send_enable_notification(username: str, delete_disable_msg: bool = False) -> None:
    """
    Send an enable notification without deleting the original disable message.

    Args:
        username: The username that was enabled
        delete_disable_msg: Whether to cancel/delete the original disable message (default: False)
    """
    from datetime import datetime

    tg_send_logger.debug(f"✅ Sending enable notification for {username}")

    if delete_disable_msg:
        await cancel_or_delete_disable_message(username)

    # Send enable notification with Priority.CRITICAL
    enable_time = datetime.now().strftime("%H:%M:%S")
    msg = f"✅ <b>User Enabled</b>\n\n👤 User: <code>{html_lib.escape(username)}</code>\n🕐 Time: <code>{enable_time}</code>"
    await send_disable_enable_log(msg)


async def delete_disable_message_for_user(username: str) -> bool:
    """Backward compatibility alias for cancel_or_delete_disable_message."""
    return await cancel_or_delete_disable_message(username)


async def send_user_message(
    msg: str,
    username: str,
    device_count: int,
    has_special_limit: bool,
    is_except: bool,
    general_limit: int = 2,
) -> None:
    """
    Send a message for a single user with inline buttons for setting limits to NO_LIMIT topic.
    Uses deduplication to avoid sending duplicate messages for the same user.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    topics_manager = get_topics_manager()
    message_key = f"no_limit:{username}"

    if (
        not topics_manager.enabled
        or not topics_manager.group_id
        or topics_manager.is_message_sent(TopicType.NO_LIMIT, message_key)
    ):
        return

    reply_markup = None
    if not has_special_limit and not is_except:
        keyboard = [
            [
                InlineKeyboardButton(
                    f"📱 Set {device_count} limit", callback_data=f"set_limit:{username}:{device_count}"
                ),
                InlineKeyboardButton("🚫 Add to except", callback_data=f"add_except:{username}"),
            ],
            [
                InlineKeyboardButton("1️⃣ Set 1 device", callback_data=f"set_limit:{username}:1"),
                InlineKeyboardButton(
                    f"🔢 Set {general_limit} (general)", callback_data=f"set_limit:{username}:{general_limit}"
                ),
            ],
            [
                InlineKeyboardButton("✏️ Custom limit", callback_data=f"custom_limit:{username}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    await send_logs(
        msg=msg,
        topic_type=TopicType.NO_LIMIT,
        priority=Priority.SYSTEM,
        reply_markup=reply_markup,
        message_key=message_key,
    )
