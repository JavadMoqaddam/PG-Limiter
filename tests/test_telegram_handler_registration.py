"""Handler-registration ordering tests.

python-telegram-bot runs at most one matching handler per group, so the broad
group-0 text handler must not sit in the same or an earlier group than the
ConversationHandlers, or it consumes their text states before they see them.
"""

from telegram.ext import ConversationHandler, MessageHandler

import telegram_bot.main as tgmain


def _text_router_group(app):
    for group, handlers in app.handlers.items():
        for handler in handlers:
            if isinstance(handler, MessageHandler) and handler.callback is tgmain.text_message_handler:
                return group
    return None


def _conversation_groups(app):
    groups = []
    for group, handlers in app.handlers.items():
        for handler in handlers:
            if isinstance(handler, ConversationHandler):
                groups.append(group)
    return groups


def test_conversation_text_not_shadowed():
    app = tgmain.application
    text_group = _text_router_group(app)
    conversation_groups = _conversation_groups(app)

    assert text_group is not None, "catch-all text handler is not registered"
    assert conversation_groups, "no ConversationHandlers registered"

    # The catch-all text router must run in a strictly later group than every
    # ConversationHandler, so an active conversation's text state wins.
    assert all(text_group > group for group in conversation_groups)
