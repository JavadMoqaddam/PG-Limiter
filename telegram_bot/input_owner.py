"""Own and scope one pending Telegram interaction per user.

Native PTB conversations retain their state transitions; ownership determines
which conversation or inline prompt may consume an inbound continuation.
"""

from dataclasses import dataclass, replace
from functools import wraps
from collections.abc import Awaitable

from telegram import Message, Update
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes, ConversationHandler

OWNER_KEY = "input_owner"
PENDING_KEYS = (
    "waiting_for",
    "selected_user",
    "limit_number",
    "limit_group_id",
    "limit_group_name",
    "cdn_mode_action",
)


@dataclass(frozen=True)
class InputOwner:
    """Origin and flow identity of a single pending interaction."""

    kind: str
    flow: str
    user_id: int
    chat_id: int
    topic_id: int | None
    prompt_id: int | None = None

    def matches(self, update: Update, *, require_prompt: bool = False) -> bool:
        """Accept only the originating user/chat/topic and any explicit prompt reply."""
        message = update.effective_message
        if not message or not update.effective_user or not update.effective_chat:
            return False
        if (
            update.effective_user.id != self.user_id
            or update.effective_chat.id != self.chat_id
            or message.message_thread_id != self.topic_id
        ):
            return False
        if require_prompt:
            return message.message_id == self.prompt_id
        if message.reply_to_message:
            return message.reply_to_message.message_id == self.prompt_id
        return True


def bind_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, flow: str, *, prompt_id: int | None = None
) -> None:
    """Assign ownership to the current interaction's origin."""
    context.user_data[OWNER_KEY] = InputOwner(
        kind=kind,
        flow=flow,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        topic_id=update.effective_message.message_thread_id,
        prompt_id=prompt_id,
    )


async def reply_input_prompt(context: ContextTypes.DEFAULT_TYPE, response: Awaitable[Message]) -> Message:
    """Bind a delivered native prompt before its continuation can run."""
    owner = context.user_data.get(OWNER_KEY)
    prompt = await response
    if owner and owner.kind == "command" and context.user_data.get(OWNER_KEY) is owner:
        context.user_data[OWNER_KEY] = replace(owner, prompt_id=prompt.message_id)
    return prompt


def end_input(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Terminate the owner and remove its temporary input payload."""
    if context.user_data is None:
        return
    owner = context.user_data.pop(OWNER_KEY, None)
    if owner and owner.kind == "command":
        for handlers in context.application.handlers.values():
            for handler in handlers:
                if isinstance(handler, ScopedConversationHandler) and handler.name == owner.flow:
                    handler.end_owner(owner)
    for key in PENDING_KEYS:
        context.user_data.pop(key, None)


async def supersede_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every command replaces the user's pending interaction before normal dispatch."""
    end_input(context)


def accepts_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, *, require_prompt: bool = False) -> bool:
    """Check inline provenance before a text/document/button continuation."""
    owner = context.user_data.get(OWNER_KEY)
    return bool(owner and owner.kind == "inline" and owner.matches(update, require_prompt=require_prompt))


class ScopedConversationHandler(ConversationHandler):
    """Gate native PTB conversations by their active owner and stop later groups."""

    def __init__(self, application: Application, **kwargs):
        self._owner_application = application
        entry_points = kwargs["entry_points"]
        kwargs.setdefault("name", entry_points[0].callback.__name__)
        kwargs["allow_reentry"] = True
        flow = kwargs["name"]

        def own_callback(callback, *, entry=False):
            @wraps(callback)
            async def owned(update: Update, context: ContextTypes.DEFAULT_TYPE):
                if entry:
                    bind_input(update, context, "command", flow)
                try:
                    state = await callback(update, context)
                except Exception:
                    end_input(context)
                    raise
                if state == ConversationHandler.END:
                    end_input(context)
                # PTB records this state before propagating the stop to Application.
                raise ApplicationHandlerStop(state)

            return owned

        for handler in entry_points:
            handler.callback = own_callback(handler.callback, entry=True)
        for handlers in kwargs["states"].values():
            for handler in handlers:
                handler.callback = own_callback(handler.callback)
        for handler in kwargs.get("fallbacks", []):
            handler.callback = own_callback(handler.callback)
        super().__init__(**kwargs)

    def check_update(self, update: object):
        """Inactive or out-of-scope states cannot swallow another owner's reply."""
        if not isinstance(update, Update) or not update.effective_user:
            return None
        if any(handler.check_update(update) for handler in self.entry_points):
            return super().check_update(update)
        owner = self._owner_application.user_data.get(update.effective_user.id, {}).get(OWNER_KEY)
        if not owner or owner.kind != "command" or owner.flow != self.name or not owner.matches(update):
            return None
        return super().check_update(update)

    def end_owner(self, owner: InputOwner) -> None:
        """Release the native state when a command/menu supersedes the interaction.

        PTB 22.8 has no public cancellation method. Keep this version-specific
        operation here; all registered conversations use per-chat/per-user keys
        and blocking callbacks without conversation timeouts.
        """
        self._update_state(self.END, (owner.chat_id, owner.user_id))
