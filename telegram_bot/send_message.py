"""
Send logs to Telegram Bot with topic support and rate-limited priority dispatching.
All messages are strictly sent to Forum Group topics without private chat fallback.
"""

import asyncio
import json
import os
import time
from typing import Optional, Tuple

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
    """Save disable messages tracking to file."""
    try:
        atomic_write_json(DISABLE_MESSAGES_FILE, data)
    except Exception as e:
        tg_send_logger.error(f"Error saving disable messages: {e}")


async def track_disable_message(username: str, message_id: int, chat_id: int) -> None:
    """Track a disable message for later deletion."""
    async with _disable_msg_lock:
        data = await asyncio.to_thread(_load_disable_messages)
        data[username] = {"message_id": message_id, "chat_id": chat_id}
        await asyncio.to_thread(_save_disable_messages, data)
    tg_send_logger.debug(f"📝 Tracked disable message for {username}: msg={message_id}, chat={chat_id}")


async def get_disable_message(username: str) -> Optional[Tuple[int, int]]:
    """Get tracked disable message for a user. Returns (message_id, chat_id) or None."""
    data = await asyncio.to_thread(_load_disable_messages)
    msg_info = data.get(username)
    if msg_info:
        return (msg_info["message_id"], msg_info["chat_id"])
    return None


async def remove_disable_message_tracking(username: str) -> None:
    """Remove tracking for a user's disable message."""
    async with _disable_msg_lock:
        data = await asyncio.to_thread(_load_disable_messages)
        if username in data:
            del data[username]
            await asyncio.to_thread(_save_disable_messages, data)
            tg_send_logger.debug(f"🗑️ Removed disable message tracking for {username}")


import html as html_lib


def _sanitize_html_message(msg: str) -> str:
    """If message contains raw HTML document tags, wrap in code tag to prevent Telegram parse errors."""
    if not isinstance(msg, str):
        msg = str(msg)
    if "<html>" in msg.lower() or "<head>" in msg.lower() or "<body>" in msg.lower():
        clean = html_lib.escape(msg)
        return f"<code>{clean[:1000]}</code>"
    return msg


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
    """
    Send logs to forum group topic via TelegramDispatcher with rate limiting and prioritization.
    
    Args:
        msg: HTML formatted message text
        return_message_id: If True, awaits delivery and returns (message_id, chat_id)
        reply_markup: Optional inline keyboard markup
        topic_type: Topic type enum
        message_key: Optional deduplication key
        priority: Priority level (Priority.CRITICAL, WARNINGS, SYSTEM)
        ttl: Optional Time-To-Live in seconds
        cancel_key: Key used to cancel message before delivery if needed
        
    Returns:
        (message_id, chat_id) or None
    """
    msg = _sanitize_html_message(msg)
    topics_manager = get_topics_manager()

    if not topics_manager.group_id:
        tg_send_logger.warning("⚠️ No forum group configured to send logs")
        return None

    if not topics_manager.enabled:
        await topics_manager.set_enabled(True)

    # Check for duplicate if message_key provided
    if message_key and topics_manager.is_message_sent(topic_type, message_key):
        tg_send_logger.debug(f"⏭️ Skipping duplicate message: {message_key[:50]}...")
        return None

    dispatcher = get_dispatcher()
    future = await dispatcher.enqueue_send(
        text=msg,
        topic_type=topic_type,
        priority=priority,
        reply_markup=reply_markup,
        ttl=ttl,
        cancel_key=cancel_key,
        return_future=return_message_id,
    )

    if message_key:
        await topics_manager.mark_message_sent(topic_type, message_key)

    if return_message_id and future:
        try:
            result = await asyncio.wait_for(future, timeout=10.0)
            return result
        except asyncio.TimeoutError:
            tg_send_logger.warning("⏱ Timeout waiting for message send result from dispatcher")
            return None
        except Exception as e:
            tg_send_logger.error(f"❌ Error waiting for message send result: {e}")
            return None

    return None


async def send_warning_log(msg: str, return_message_id: bool = False, reply_markup: Any = None, ttl: Optional[float] = None) -> Optional[Tuple[int, int]]:
    """Send a warning message to the warnings topic with Priority.WARNINGS."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.WARNINGS, priority=Priority.WARNINGS, ttl=ttl)


async def send_disable_enable_log(msg: str, return_message_id: bool = False, reply_markup: Any = None) -> Optional[Tuple[int, int]]:
    """Send a disable/enable message to the disable_enable topic with Priority.CRITICAL."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.DISABLE_ENABLE, priority=Priority.CRITICAL)


async def send_backup_log(msg: str, return_message_id: bool = False, reply_markup: Any = None) -> Optional[Tuple[int, int]]:
    """Send a backup message to the backups topic with Priority.SYSTEM."""
    return await send_logs(msg, return_message_id, reply_markup, TopicType.BACKUPS, priority=Priority.SYSTEM)


async def send_no_limit_log(msg: str, return_message_id: bool = False, reply_markup: Any = None) -> Optional[Tuple[int, int]]:
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
            return bool(await asyncio.wait_for(future, timeout=10.0))
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
            return bool(await asyncio.wait_for(future, timeout=10.0))
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
    Guaranteed delivery with Priority.CRITICAL and tracks message ID for deletion.
    
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
    message_info = await send_logs(
        msg,
        return_message_id=True,
        reply_markup=reply_markup,
        topic_type=TopicType.DISABLE_ENABLE,
        priority=Priority.CRITICAL,
        cancel_key=cancel_key,
    )

    if message_info:
        await track_disable_message(username, message_info[0], message_info[1])


async def cancel_or_delete_disable_message(username: str) -> bool:
    """
    Dual cancel/delete workflow:
    1. If a disable message is still pending in the dispatcher queue, cancel it.
    2. If it was already sent to Telegram, delete it and clear tracking.
    
    Args:
        username: Username to cancel or delete disable notification for
        
    Returns:
        True if cancelled or deleted, False otherwise
    """
    dispatcher = get_dispatcher()
    cancel_key = f"disable:{username}"

    # 1. Try cancelling pending queue item
    if dispatcher.cancel_pending(cancel_key):
        await remove_disable_message_tracking(username)
        tg_send_logger.info(f"🚫 Cancelled pending disable message in queue for {username}")
        return True

    # 2. If already sent to Telegram, delete it
    message_info = await get_disable_message(username)
    if message_info:
        deleted = await delete_message(message_info)
        await remove_disable_message_tracking(username)
        if deleted:
            tg_send_logger.info(f"🗑️ Deleted disable message for {username}")
        return deleted

    return False


async def send_enable_notification(username: str, delete_disable_msg: bool = True) -> None:
    """
    Send an enable notification and optionally cancel/delete the original disable message.
    
    Args:
        username: The username that was enabled
        delete_disable_msg: Whether to cancel/delete the original disable message
    """
    from datetime import datetime

    tg_send_logger.debug(f"✅ Sending enable notification for {username}")

    if delete_disable_msg:
        await cancel_or_delete_disable_message(username)

    # Send enable notification with Priority.CRITICAL
    enable_time = datetime.now().strftime("%H:%M:%S")
    msg = f"✅ <b>User Enabled</b>\n\n👤 User: <code>{username}</code>\n🕐 Time: <code>{enable_time}</code>"
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

    if not topics_manager.group_id or topics_manager.is_message_sent(TopicType.NO_LIMIT, message_key):
        return

    reply_markup = None
    if not has_special_limit and not is_except:
        keyboard = [
            [
                InlineKeyboardButton(f"📱 Set {device_count} limit", callback_data=f"set_limit:{username}:{device_count}"),
                InlineKeyboardButton("🚫 Add to except", callback_data=f"add_except:{username}"),
            ],
            [
                InlineKeyboardButton("1️⃣ Set 1 device", callback_data=f"set_limit:{username}:1"),
                InlineKeyboardButton(f"🔢 Set {general_limit} (general)", callback_data=f"set_limit:{username}:{general_limit}"),
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
