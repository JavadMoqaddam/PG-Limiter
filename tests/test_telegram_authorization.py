"""Authorization tests for stateful continuations.

Authorization checked only at a conversation's entry point is not enough: a
revoked admin can still send the reply that completes a mutation. Every
mutating continuation must re-check privilege and end the flow on failure.
"""

from unittest.mock import AsyncMock, MagicMock

from telegram.ext import ConversationHandler

import telegram_bot.handlers.admin as admin_mod
import telegram_bot.main as tgmain


async def test_revoked_admin_cannot_complete_mutation(monkeypatch):
    mutation = AsyncMock(return_value="MUTATED")
    guarded = admin_mod.require_admin_continuation(mutation)

    # Privilege re-check now denies (user was removed since the flow started).
    monkeypatch.setattr(
        admin_mod, "check_admin_privilege", AsyncMock(return_value=ConversationHandler.END)
    )

    update = MagicMock()
    context = MagicMock()
    context.user_data = {"waiting_for": "get_chat_id", "pending": 1}

    result = await guarded(update, context)

    mutation.assert_not_awaited()
    assert result == ConversationHandler.END
    assert context.user_data == {}


async def test_revoked_admin_blocked_in_text_router(monkeypatch):
    monkeypatch.setattr(
        tgmain, "check_admin_privilege", AsyncMock(return_value=ConversationHandler.END)
    )
    mutation = AsyncMock()
    monkeypatch.setattr(tgmain, "handle_general_limit_input", mutation)

    update = MagicMock()
    context = MagicMock()
    context.user_data = {"waiting_for": "general_limit"}

    await tgmain.text_message_handler(update, context)

    mutation.assert_not_awaited()
    assert context.user_data.get("waiting_for") is None
