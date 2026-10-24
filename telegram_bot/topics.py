"""
Telegram Topics Management for PG-Limiter.
Manages forum topics in a forum supergroup for organizing bot messages.

Note: Telegram Bot API requires forum topics to be created in supergroups
with topics enabled (is_forum=True), not in private chats.

Topics:
- General: Settings, actions, and general messages
- Warnings: User warning notifications
- Disable/Enable: User disable/enable notifications
- Active Users: Active users log and reports
- Backups: Automatic backup notifications
- No Limit Found: Users without special limits
"""

import asyncio
import json
import os
import time
from enum import Enum
from typing import Optional
from utils.logs import get_logger

topics_logger = get_logger("topics")

# Topics data file
TOPICS_FILE = "data/topics.json"

# Message cache file for deduplication
MESSAGE_CACHE_FILE = "data/topic_message_cache.json"

# Cache expiry in seconds (24 hours)
MESSAGE_CACHE_EXPIRY = 86400

from utils.atomic_io import atomic_write_json


class TopicType(Enum):
    """Topic types for message categorization."""

    GENERAL = "general"
    WARNINGS = "warnings"
    DISABLE_ENABLE = "disable_enable"
    BACKUPS = "backups"
    NO_LIMIT = "no_limit"


# Topic display configuration
TOPIC_CONFIG = {
    TopicType.GENERAL: {
        "name": "⚙️ General",
        "icon_color": 0x6FB9F0,  # Blue
        "description": "Settings, actions, and general messages",
    },
    TopicType.WARNINGS: {
        "name": "⚠️ Warnings",
        "icon_color": 0xFFD67E,  # Yellow
        "description": "User warning notifications",
    },
    TopicType.DISABLE_ENABLE: {
        "name": "🔒 Disable/Enable",
        "icon_color": 0xCB86DB,  # Purple
        "description": "User disable and enable notifications",
    },
    TopicType.BACKUPS: {
        "name": "💾 Backups",
        "icon_color": 0xFF93B2,  # Pink
        "description": "Automatic backup notifications",
    },
    TopicType.NO_LIMIT: {
        "name": "📱 No Limit Found",
        "icon_color": 0xFB6F5F,  # Orange-red
        "description": "Users without special limits",
    },
}


class TopicsManager:
    """Own one atomically persisted forum destination and delivered-key snapshot."""

    def __init__(self):
        self._topics: dict[str, int] = {}
        self._group_id: Optional[int] = None
        self._enabled: bool = False
        self._monitoring_revision = 0
        self._destination_revision = 0
        self._message_cache: dict[str, dict] = {}
        self._write_lock = asyncio.Lock()
        self._load()

    def _load(self):
        """Load the snapshot, importing the separate cache only for legacy settings."""
        try:
            if not os.path.exists(TOPICS_FILE):
                return
            with open(TOPICS_FILE, "r", encoding="utf-8") as source:
                data = json.load(source)
            self._publish(data)
            if "message_cache" not in data and os.path.exists(MESSAGE_CACHE_FILE):
                try:
                    with open(MESSAGE_CACHE_FILE, "r", encoding="utf-8") as source:
                        self._message_cache = json.load(source)
                    self._clean_expired_cache()
                except Exception as error:
                    topics_logger.error(f"❌ Failed to load legacy message cache: {error}")
                    self._message_cache = {}
            self._clean_expired_cache()
            topics_logger.debug(f"📁 Loaded topics config: enabled={self._enabled}, group={self._group_id}")
        except Exception as error:
            topics_logger.error(f"❌ Failed to load topics: {error}")
            self._publish({})

    def _publish(self, data: dict) -> None:
        self._topics = data.get("topics", {})
        self._enabled = data.get("enabled", False)
        self._group_id = data.get("group_id")
        self._message_cache = data.get("message_cache", {})
        self._monitoring_revision = data.get("monitoring_revision", 0)
        self._destination_revision = data.get("destination_revision", 0)

    def _snapshot(self) -> dict:
        return {
            "version": 2,
            "enabled": self._enabled,
            "group_id": self._group_id,
            "topics": self._topics.copy(),
            "message_cache": {topic: cache.copy() for topic, cache in self._message_cache.items()},
            "monitoring_revision": self._monitoring_revision,
            "destination_revision": self._destination_revision,
        }

    async def _update(self, mutate) -> bool:
        """Persist a detached candidate before publishing it under the mutation lock."""
        async with self._write_lock:
            candidate = self._snapshot()
            mutate(candidate)
            if candidate == self._snapshot():
                return False
            writer = asyncio.create_task(asyncio.to_thread(atomic_write_json, TOPICS_FILE, candidate))
            cancelled = False
            while not writer.done():
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    # Every wait must stay shielded: another cancellation cannot
                    # stop the thread or release the lock before publication.
                    cancelled = True
            writer.result()
            self._publish(candidate)
            if cancelled:
                raise asyncio.CancelledError
            return True

    def _clean_expired_cache(self):
        """Remove expired entries from the loaded snapshot."""
        current_time = time.time()
        for topic_type in list(self._message_cache):
            topic_cache = self._message_cache[topic_type]
            expired_keys = [
                key for key, timestamp in topic_cache.items() if current_time - timestamp > MESSAGE_CACHE_EXPIRY
            ]
            for key in expired_keys:
                del topic_cache[key]
            if not topic_cache:
                del self._message_cache[topic_type]

    def is_message_sent(self, topic_type: TopicType, message_key: str) -> bool:
        """Check the current destination's unexpired delivered-key cache."""
        if not self.monitoring_allowed:
            return False
        timestamp = self._message_cache.get(topic_type.value, {}).get(message_key)
        return timestamp is not None and time.time() - timestamp < MESSAGE_CACHE_EXPIRY

    async def mark_message_sent(
        self,
        topic_type: TopicType,
        message_key: str,
        *,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        monitoring_revision: Optional[int] = None,
        destination_revision: Optional[int] = None,
    ) -> None:
        """Mark a receipt only if its captured destination still matches under the lock."""

        def mark(candidate):
            if not candidate["enabled"] or not candidate["group_id"]:
                return
            if monitoring_revision is not None and candidate["monitoring_revision"] != monitoring_revision:
                return
            if destination_revision is not None and candidate["destination_revision"] != destination_revision:
                return
            if chat_id is not None:
                thread_id = candidate["topics"].get(topic_type.value) or candidate["topics"].get(
                    TopicType.GENERAL.value
                )
                if candidate["group_id"] != chat_id or thread_id != message_thread_id:
                    return
            candidate["message_cache"].setdefault(topic_type.value, {})[message_key] = time.time()

        await self._update(mark)

    async def clear_message_cache(self, topic_type: Optional[TopicType] = None):
        """Atomically clear delivered keys for one topic or the entire destination."""

        def clear(candidate):
            if topic_type:
                candidate["message_cache"].pop(topic_type.value, None)
            else:
                candidate["message_cache"] = {}
            candidate["destination_revision"] += 1

        await self._update(clear)

    @property
    def enabled(self) -> bool:
        """Check whether the operator enabled topics and monitoring."""
        return self._enabled

    @property
    def group_id(self) -> Optional[int]:
        """Get the configured forum group ID."""
        return self._group_id

    @property
    def monitoring_allowed(self) -> bool:
        """Synchronous gate shared by log producers and the monitoring lifecycle."""
        return self._enabled and self._group_id is not None

    @property
    def monitoring_revision(self) -> int:
        """Identify observations admitted since the latest explicit monitoring pause."""
        return self._monitoring_revision

    @property
    def destination_revision(self) -> int:
        """Identify the destination namespace across group/thread/cache replacements."""
        return self._destination_revision

    @property
    def monitoring_block_reason(self) -> Optional[str]:
        """Explain why monitoring cannot start or continue, if it is blocked."""
        if not self._enabled:
            return "Topics are disabled. Monitoring is paused and logs are not queued."
        if self._group_id is None:
            return "No forum group is configured. Monitoring is paused and logs are not queued."
        return None

    async def set_group_id(self, group_id: int):
        """Replace a destination together with its group-local topics and delivered keys."""

        def replace(candidate):
            if candidate["group_id"] != group_id:
                candidate["group_id"] = group_id
                candidate["topics"] = {}
                candidate["message_cache"] = {}
                candidate["destination_revision"] += 1

        if await self._update(replace):
            topics_logger.info(f"📌 Forum group set to {group_id}")

    async def set_enabled(self, enabled: bool):
        """Persist the operator's explicit topic and monitoring preference."""

        def toggle(candidate):
            if candidate["enabled"] and not enabled:
                candidate["monitoring_revision"] += 1
                candidate["message_cache"] = {}
            candidate["enabled"] = enabled

        await self._update(toggle)
        topics_logger.info(f"📌 Topics {'enabled' if enabled else 'disabled'}")

    def get_topic_id(self, topic_type: TopicType) -> Optional[int]:
        """Get a group-local thread only when topics and monitoring are enabled."""
        if not self.monitoring_allowed:
            return None
        return self._topics.get(topic_type.value)

    async def set_topic_id(
        self,
        topic_type: TopicType,
        thread_id: int,
        *,
        group_id: Optional[int] = None,
        destination_revision: Optional[int] = None,
    ) -> bool:
        """Install a thread only in the destination for which it was created."""

        def install(candidate):
            if group_id is not None and candidate["group_id"] != group_id:
                return
            if destination_revision is not None and candidate["destination_revision"] != destination_revision:
                return
            if candidate["topics"].get(topic_type.value) != thread_id:
                candidate["topics"][topic_type.value] = thread_id
                candidate["destination_revision"] += 1
                # A General thread may be the fallback for every category.
                if topic_type == TopicType.GENERAL:
                    candidate["message_cache"] = {}
                else:
                    candidate["message_cache"].pop(topic_type.value, None)

        changed = await self._update(install)
        if changed:
            topics_logger.info(f"📌 Set {topic_type.value} topic to thread {thread_id}")
        return changed

    async def remove_topic(self, topic_type: TopicType):
        """Remove a topic mapping and the receipts scoped to its thread."""

        def remove(candidate):
            if topic_type.value not in candidate["topics"]:
                return
            candidate["topics"].pop(topic_type.value)
            candidate["destination_revision"] += 1
            if topic_type == TopicType.GENERAL:
                candidate["message_cache"] = {}
            else:
                candidate["message_cache"].pop(topic_type.value, None)

        await self._update(remove)

    async def clear_all_topics(self):
        """Atomically clear thread mappings and their delivered keys."""

        def clear(candidate):
            if candidate["topics"] or candidate["message_cache"]:
                candidate.update(topics={}, message_cache={})
                candidate["destination_revision"] += 1

        await self._update(clear)

    def get_all_topics(self) -> dict[str, int]:
        """Get a copy of the current group's thread mappings."""
        return self._topics.copy()

    async def check_bot_permissions(self, bot, group_id: Optional[int] = None) -> tuple[bool, str]:
        """Validate a proposed forum group without changing the active destination."""
        proposed_group = self._group_id if group_id is None else group_id
        if not proposed_group:
            return False, "No forum group configured. Please set a group ID first."
        try:
            chat = await bot.get_chat(proposed_group)
            if not getattr(chat, "is_forum", False):
                return False, (
                    "The specified chat is not a forum supergroup.\nPlease enable Topics in the group settings first."
                )
            bot_member = await bot.get_chat_member(proposed_group, bot.id)
            if bot_member.status not in ["administrator", "creator"]:
                return False, (
                    "The bot is not an administrator in the group.\n"
                    "Please make the bot an admin with 'Manage Topics' permission."
                )
            if bot_member.status == "administrator" and not getattr(bot_member, "can_manage_topics", False):
                return False, (
                    "The bot doesn't have 'Manage Topics' permission.\nPlease enable this permission for the bot."
                )
            return True, f"✅ Bot has required permissions in group: {chat.title}"
        except Exception as error:
            return False, f"Failed to check permissions: {error}"

    async def create_topics_for_group(self, bot) -> dict[TopicType, int]:
        """Create topics for one captured group; never install another group's threads."""
        group_id = self._group_id
        destination_revision = self._destination_revision
        if not group_id:
            topics_logger.error("❌ No forum group configured")
            return {}
        success, message = await self.check_bot_permissions(bot, group_id=group_id)
        if not success:
            topics_logger.error(f"❌ Permission check failed: {message}")
            return {}
        if self._destination_revision != destination_revision:
            return {}
        created_topics = {}
        for topic_type, config in TOPIC_CONFIG.items():
            if self._group_id != group_id:
                break
            destination_revision = self._destination_revision
            try:
                forum_topic = await bot.create_forum_topic(
                    chat_id=group_id, name=config["name"], icon_color=config["icon_color"]
                )
            except Exception as error:
                topics_logger.error(f"❌ Failed to create topic '{config['name']}': {error}")
                if self._destination_revision != destination_revision:
                    return {}
                continue
            thread_id = forum_topic.message_thread_id
            if not await self.set_topic_id(
                topic_type, thread_id, group_id=group_id, destination_revision=destination_revision
            ):
                return {}
            created_topics[topic_type] = thread_id
            topics_logger.info(f"✅ Created topic '{config['name']}' (thread_id={thread_id})")
        return created_topics if self._group_id == group_id else {}


# Global instance
_topics_manager: Optional[TopicsManager] = None


def get_topics_manager() -> TopicsManager:
    """Get or create the global topics manager instance."""
    global _topics_manager
    if _topics_manager is None:
        _topics_manager = TopicsManager()
    return _topics_manager


async def send_to_topic(
    bot,
    text: str,
    topic_type: TopicType,
    parse_mode: str = "HTML",
    reply_markup=None,
    message_key: Optional[str] = None,
    **kwargs,
):
    """
    Send a message to a specific topic in the forum group.
    Falls back to None if topics not enabled or topic not found.

    Args:
        bot: Telegram Bot instance
        text: Message text
        topic_type: Type of topic to send to
        parse_mode: Message parse mode
        reply_markup: Optional keyboard markup
        message_key: Optional key for deduplication (if provided, message won't be sent if already sent with same key)
        **kwargs: Additional arguments for send_message

    Returns:
        Message object or None
    """
    manager = get_topics_manager()

    if not manager.enabled or not manager.group_id:
        return None

    # Check for duplicate message if message_key provided
    if message_key and manager.is_message_sent(topic_type, message_key):
        topics_logger.debug(f"⏭️ Skipping duplicate message: {message_key[:50]}...")
        return None

    chat_id = manager.group_id
    thread_id = manager.get_topic_id(topic_type) or manager.get_topic_id(TopicType.GENERAL)
    monitoring_revision = manager.monitoring_revision
    destination_revision = manager.destination_revision

    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            message_thread_id=thread_id,
            **kwargs,
        )

        # Mark message as sent if key provided
        if message_key:
            await manager.mark_message_sent(
                topic_type,
                message_key,
                chat_id=chat_id,
                message_thread_id=thread_id,
                monitoring_revision=monitoring_revision,
                destination_revision=destination_revision,
            )

        return message
    except Exception as e:
        topics_logger.warning(f"⚠️ Failed to send to topic {topic_type.value}: {e}")
        return None


async def send_document_to_topic(bot, document, topic_type: TopicType, **kwargs):
    """
    Send a document to a specific topic in the forum group.

    Args:
        bot: Telegram Bot instance
        document: Document to send
        topic_type: Type of topic to send to
        **kwargs: Additional arguments for send_document

    Returns:
        Message object or None
    """
    manager = get_topics_manager()

    if not manager.enabled or not manager.group_id:
        return None

    thread_id = manager.get_topic_id(topic_type)

    try:
        return await bot.send_document(
            chat_id=manager.group_id, document=document, message_thread_id=thread_id, **kwargs
        )
    except Exception as e:
        topics_logger.warning(f"⚠️ Failed to send document to topic {topic_type.value}: {e}")
        return None
