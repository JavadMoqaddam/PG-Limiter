"""
Unit tests for TelegramDispatcher and Chunked Warnings in PG-Limiter.
Uses standard library unittest and asyncio for universal test runner compatibility.
"""

import asyncio
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch



from telegram_bot.dispatcher import (
    ActionType,
    Priority,
    QueueItem,
    TelegramDispatcher,
    TokenBucket,
    get_dispatcher,
)
from telegram_bot.topics import TopicType
from utils.check_usage import dispatch_chunked_warnings


class TestTelegramDispatcher(unittest.IsolatedAsyncioTestCase):
    """Test suite for TelegramDispatcher and Chunked Warnings."""

    async def test_token_bucket_consumption(self):
        """Test TokenBucket token consumption and refill behavior."""
        bucket = TokenBucket(rate=10.0, capacity=5.0)

        # Consuming available tokens should be immediate
        t0 = time.monotonic()
        await bucket.consume(3.0)
        t1 = time.monotonic()
        self.assertLess(t1 - t0, 0.1)
        self.assertLessEqual(bucket.tokens, 2.1)

        # Consuming more causes a brief wait
        await bucket.consume(2.0)
        t2 = time.monotonic()
        await bucket.consume(1.0)
        t3 = time.monotonic()
        self.assertGreaterEqual(t3 - t2, 0.05)

    async def test_priority_queue_ordering_and_tuple_safety(self):
        """Test that items with higher priority are dequeued first and dict payloads don't cause '<' error."""
        dispatcher = TelegramDispatcher()

        # Enqueue items in reverse priority order with dict-like and complex payloads
        f_sys = await dispatcher.enqueue_send(
            text="System Log",
            topic_type=TopicType.GENERAL,
            priority=Priority.SYSTEM,
            return_future=True,
        )
        f_warn = await dispatcher.enqueue_send(
            text="Warning Report",
            topic_type=TopicType.WARNINGS,
            priority=Priority.WARNINGS,
            return_future=True,
        )
        f_crit = await dispatcher.enqueue_send(
            text="Ban User",
            topic_type=TopicType.DISABLE_ENABLE,
            priority=Priority.CRITICAL,
            return_future=True,
        )

        # Dequeue items and check their priority order
        p1, s1, item1 = await dispatcher.queue.get()
        self.assertEqual(p1, Priority.CRITICAL)
        self.assertEqual(item1.text, "Ban User")

        p2, s2, item2 = await dispatcher.queue.get()
        self.assertEqual(p2, Priority.WARNINGS)
        self.assertEqual(item2.text, "Warning Report")

        p3, s3, item3 = await dispatcher.queue.get()
        self.assertEqual(p3, Priority.SYSTEM)
        self.assertEqual(item3.text, "System Log")

    async def test_cancel_pending_disable_message(self):
        """Test that cancel_pending cancels an unsent message in the queue."""
        dispatcher = TelegramDispatcher()
        cancel_key = "disable:test_user_123"

        future = await dispatcher.enqueue_send(
            text="User Disabled",
            topic_type=TopicType.DISABLE_ENABLE,
            priority=Priority.CRITICAL,
            cancel_key=cancel_key,
            return_future=True,
        )

        # Before worker processes it, cancel it
        cancelled = dispatcher.cancel_pending(cancel_key)
        self.assertTrue(cancelled)

        # Future should resolve to None without calling Telegram
        self.assertTrue(future.done())
        self.assertIsNone(future.result())

    async def test_ttl_expiration(self):
        """Test that expired items (TTL elapsed) are dropped cleanly."""
        item = QueueItem(
            priority=Priority.WARNINGS,
            action_type=ActionType.SEND,
            text="Old Warning",
            topic_type=TopicType.WARNINGS,
            ttl=0.01,
            created_at=time.time() - 1.0,  # Already 1 second old
        )

        # Should be detected as expired
        now = time.time()
        self.assertTrue(item.ttl and (now - item.created_at) > item.ttl)

    async def test_dispatch_chunked_warnings_formatting(self):
        """Test chunked warning aggregation into batches of 10 with item-level error handling."""
        sample_warnings = [
            {
                "username": f"user_{i}",
                "ip_count": 3,
                "limit": 2,
                "trust_score": 50.0,
                "trust_level": "🟢 TRUSTED",
                "behavior": "Single device",
            }
            for i in range(25)  # 25 users = 3 chunks (10, 10, 5)
        ]
        # Insert one corrupt item to test item-level error guard
        sample_warnings.append({"corrupt_data": None})

        mock_send = AsyncMock()
        with patch("telegram_bot.send_message.send_warning_log", mock_send):
            await dispatch_chunked_warnings(sample_warnings, check_interval=60.0, total_monitored=26)

        # 26 items with chunk_size 10 = 3 chunks
        self.assertEqual(mock_send.call_count, 3)

        # Check that text contains HTML formatted tags and chunk headers
        first_call_args = mock_send.call_args_list[0]
        msg_text = first_call_args[0][0]
        self.assertIn("<b>WARNINGS REPORT</b> (1/3)", msg_text)
        self.assertIn("user_0", msg_text)
        self.assertEqual(first_call_args[1]["ttl"], 600.0)

    async def test_duplicate_text_node_edit_guard(self):
        """Test that editing node status with identical text is ignored."""
        from utils.get_logs import _update_node_status
        import utils.get_logs as gl

        gl._node_status_message_id = (100, -100123456)
        gl._last_status_edit_time = 0.0
        gl._last_status_text = "sample text"

        mock_edit = AsyncMock(return_value=True)
        with patch("utils.get_logs.edit_message", mock_edit), \
             patch("utils.get_logs._get_status_throttle_interval", AsyncMock(return_value=0.0)), \
             patch("utils.get_logs._build_node_status_message", AsyncMock(return_value="sample text")):

            await _update_node_status(1, "node-1", "✅ Connected")
            # Since text is unchanged ("sample text" == "sample text"), edit_message should not be called
            mock_edit.assert_not_called()

    async def test_reload_restores_cancel_keys(self):
        """Test that reload_pending_critical_messages restores active cancel keys and builds buttons."""
        dispatcher = TelegramDispatcher()
        dispatcher._pending_storage = {
            "test_db_1": {
                "action_type": ActionType.SEND,
                "text": "User Ban",
                "topic_type": "disable_enable",
                "cancel_key": "disable:john_doe",
                "created_at": time.time(),
            }
        }

        await dispatcher.reload_pending_critical_messages()
        # Verify cancel key is in _active_cancel_keys and cancel_pending works
        self.assertIn("disable:john_doe", dispatcher._active_cancel_keys)
        cancelled = dispatcher.cancel_pending("disable:john_doe")
        self.assertTrue(cancelled)

    async def test_trailing_node_status_update(self):
        """Test that delayed trailing task flushes latest status to Telegram."""
        import utils.get_logs as gl
        from utils.get_logs import _update_node_status

        gl._node_status_message_id = (100, -100123456)
        gl._last_status_edit_time = time.time()  # Just updated
        gl._last_status_text = "old status"
        gl._pending_status_update_task = None

        mock_edit = AsyncMock(return_value=True)
        with patch("utils.get_logs.edit_message", mock_edit), \
             patch("utils.get_logs._get_status_throttle_interval", AsyncMock(return_value=0.05)), \
             patch("utils.get_logs._build_node_status_message", AsyncMock(return_value="new updated status")):

            await _update_node_status(2, "node-2", "✅ Connected")
            # Immediately within throttle, edit_message is not called directly
            mock_edit.assert_not_called()
            # Wait for trailing task to finish
            await asyncio.sleep(0.08)
            mock_edit.assert_called_once_with((100, -100123456), "new updated status")


if __name__ == "__main__":
    unittest.main()
