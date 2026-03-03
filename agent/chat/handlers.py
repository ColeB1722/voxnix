"""Telegram message handlers for the voxnix agent chat layer.

Responsibilities:
  - Enforce access control via chat_id allowlist (gate handler, #61)
  - Extract the Telegram chat_id as the owner identity (trust model anchor)
  - Dispatch text messages to the PydanticAI agent
  - Maintain per-chat conversation history via ConversationStore
  - Split long agent responses into Telegram-sized chunks
  - Handle errors gracefully — users never see raw tracebacks

Architecture decisions reflected here:
  - Access control is fail-closed: if TELEGRAM_ALLOWED_CHAT_IDS is empty or unset,
    ALL users are denied. The bot is locked by default. See #61.
  - chat_id IS the user identity — no separate auth system (see docs/architecture.md § Trust Model)
  - agent.run() is called with owner=str(chat_id), which flows into VoxnixDeps
  - Conversation history is stored in-memory per chat_id with TTL expiry and turn limits.
    The store lives for the process lifetime — lost on restart, which is acceptable for
    infrastructure commands. See #48 and #62.
  - The handler is the boundary between Telegram and the agent — it owns error handling
  - Typing action is sent before the agent runs so the user sees immediate feedback

See docs/architecture.md § Chat Integration Layer and § Trust Model.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.constants import ChatAction

from agent.agent import run as agent_run  # re-exported for easy mocking in tests
from agent.chat.history import ConversationStore
from agent.config import get_settings

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_MESSAGE_LEN: int = 4096

# Default conversation history settings.
# 30 minutes TTL — conversations go stale after inactivity.
# Context window trimming is handled by the agent's history_processors,
# not the store. See agent.py § keep_recent_turns.
DEFAULT_TTL_SECONDS: float = 1800.0


# ── Owner extraction ──────────────────────────────────────────────────────────


def owner_from_update(update: Update) -> str:
    """Extract the Telegram chat_id as the owner identity string.

    The chat_id is the trust model anchor — it uniquely identifies the user
    (or group) and is verified by Telegram before the message reaches the bot.
    We stringify it immediately so the rest of the system deals with plain str.

    Args:
        update: The incoming Telegram update.

    Returns:
        str(update.effective_chat.id) — always a non-empty string.
    """
    if update.effective_chat is None:
        raise ValueError("owner_from_update called on an update with no effective_chat")
    return str(update.effective_chat.id)


# ── Conversation history ──────────────────────────────────────────────────────


def _get_conversation_store(application: Application) -> ConversationStore:
    """Return the shared ConversationStore, creating it if needed.

    The store is kept in ``application.bot_data["conversation_store"]`` so it
    lives for the process lifetime alongside the per-chat locks.

    Args:
        application: The running PTB Application instance.

    Returns:
        The singleton ConversationStore for this application.
    """
    store = application.bot_data.get("conversation_store")
    if store is None:
        store = ConversationStore(
            ttl_seconds=DEFAULT_TTL_SECONDS,
        )
        application.bot_data["conversation_store"] = store
    return store


# ── Per-chat locking ──────────────────────────────────────────────────────────


def _get_chat_lock(application: Application, chat_id: int) -> asyncio.Lock:
    """Return the asyncio.Lock for the given chat_id, creating it if needed.

    Locks are stored in application.bot_data["chat_locks"] — a dict that lives
    for the lifetime of the Application and is shared across all handler
    invocations. This is PTB's canonical mechanism for shared per-bot state.

    Because PTB's event loop is single-threaded asyncio, the dict read/write
    between await points is safe with no additional synchronisation needed.

    Args:
        application: The running PTB Application instance.
        chat_id: The Telegram chat ID to look up.

    Returns:
        The asyncio.Lock for this chat_id (same object on every call).
    """
    locks: dict[int, asyncio.Lock] = application.bot_data.setdefault("chat_locks", {})
    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()
    return locks[chat_id]


# ── Response formatting ───────────────────────────────────────────────────────


def format_response(text: str) -> list[str]:
    """Split an agent response into chunks that fit within Telegram's message limit.

    Splitting strategy:
      1. If the text fits in one message, return it as-is.
      2. Otherwise, try to split on the last newline within the window — this
         preserves readable formatting (bullet lists, log lines, etc.).
      3. If no newline is found in the window, hard-split at the limit.

    Empty string is returned as a single-element list so the caller always
    has at least one chunk to send.

    Args:
        text: The full agent response string.

    Returns:
        A list of strings, each at most TELEGRAM_MAX_MESSAGE_LEN characters.
    """
    if len(text) <= TELEGRAM_MAX_MESSAGE_LEN:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > TELEGRAM_MAX_MESSAGE_LEN:
        window = remaining[:TELEGRAM_MAX_MESSAGE_LEN]

        # Try to split on the last newline within the window for clean breaks.
        split_at = window.rfind("\n")
        if split_at > 0:
            # Split just before the newline — don't include it in the chunk.
            chunk = remaining[:split_at]
            remaining = remaining[split_at + 1 :]  # skip the newline itself
        else:
            # No newline found — hard split at the limit.
            chunk = window
            remaining = remaining[TELEGRAM_MAX_MESSAGE_LEN:]

        chunks.append(chunk)

    # Append whatever is left (guaranteed ≤ TELEGRAM_MAX_MESSAGE_LEN).
    if remaining:
        chunks.append(remaining)

    return chunks


# ── Access control ────────────────────────────────────────────────────────────


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gate handler — block unauthorized chat IDs before any other handler runs.

    Registered in handler group -1 (runs before group 0 where /start, /help,
    and the agent dispatch live). If the chat_id is not in the allowlist,
    raises ApplicationHandlerStop to prevent all downstream processing.

    Security model (fail-closed):
      - Allowlist empty or unset → deny everyone. The bot is locked by default.
      - Allowlist configured → only listed chat_ids proceed.

    Unauthorized attempts are logged at WARNING level with the chat_id so the
    admin can see who is trying to reach the bot (and add them if desired).

    This handler does NOT reply to unauthorized users — silent drop is safer
    than acknowledging the bot exists to strangers.

    See docs/architecture.md § Trust Model and issue #61.
    """
    from telegram.ext import ApplicationHandlerStop

    if update.effective_chat is None:
        raise ApplicationHandlerStop

    chat_id = update.effective_chat.id
    settings = get_settings()

    if not settings.is_chat_authorized(chat_id):
        logger.warning(
            "Access denied for chat_id=%d (not in TELEGRAM_ALLOWED_CHAT_IDS)",
            chat_id,
        )
        raise ApplicationHandlerStop


# ── Handlers ──────────────────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an incoming text message from a Telegram user.

    Flow:
      1. Guard: skip non-text and whitespace-only messages silently.
      2. Extract owner identity from the chat_id.
      3. Send a 'typing' chat action for immediate visual feedback.
      4. Run the agent with the message text and owner.
      5. Split and send the response back.
      6. On any exception, reply with a friendly error message — never propagate.

    Args:
        update: The incoming Telegram update.
        context: PTB handler context (provides context.bot for API calls).
    """
    # 1. Guard — only act on non-empty text messages.
    text = update.effective_message.text if update.effective_message else None
    if not text or not text.strip():
        return

    # Both are guaranteed non-None because we have a text message at this point.
    # Use explicit raises rather than assert — assertions are stripped by python -O.
    if update.effective_message is None:
        raise ValueError("handle_message called on an update with no effective_message")
    if update.effective_chat is None:
        raise ValueError("handle_message called on an update with no effective_chat")

    owner = owner_from_update(update)
    chat_id = update.effective_chat.id

    # 2. Acquire the per-chat lock — serialises concurrent messages from the
    #    same user so they never race against each other or spawn parallel
    #    agent runs. Different chat_ids get independent locks and run freely.
    lock = _get_chat_lock(context.application, chat_id)
    async with lock:
        # 3. Send typing indicator once we hold the lock — this way it only
        #    fires when the agent is actually about to process, not while the
        #    message is sitting in the queue waiting for a previous run.
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # 4. Retrieve conversation history for this chat.
        store = _get_conversation_store(context.application)
        history = store.get(owner)

        # 5. Run the agent — catch all exceptions to keep the bot alive and
        #    to ensure the lock is always released (async with guarantees this).
        try:
            response, new_messages = await agent_run(
                text.strip(), owner=owner, message_history=history
            )
        except Exception:
            logger.exception("Agent run failed for owner=%s", owner)
            await update.effective_message.reply_text(  # asserted non-None above
                "⚠️ Something went wrong processing your request. Please try again."
            )
            return

        # 6. Persist the new messages from this turn into the conversation store.
        store.append(owner, new_messages)

        # 7. Send the response, splitting at the Telegram message limit if needed.
        chunks = format_response(response)
        for chunk in chunks:
            await update.effective_message.reply_text(chunk)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command.

    Sends a welcome message explaining what voxnix can do. Does not invoke
    the agent — this is a static informational reply.

    Args:
        update: The incoming Telegram update.
        context: PTB handler context (unused, present for handler signature).
    """
    welcome = (
        "👋 Welcome to Voxnix!\n\n"
        "I'm your personal NixOS infrastructure orchestrator. "
        "Talk to me in plain language to manage containers on your appliance.\n\n"
        "Try:\n"
        "• Spin up a dev container with git and fish\n"
        "• List my containers\n"
        "• Stop container dev-abc\n"
        "• Destroy container dev-abc\n\n"
        "Type /help for more information."
    )
    if update.effective_message is None:
        raise ValueError("handle_start called on an update with no effective_message")
    await update.effective_message.reply_text(welcome)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command.

    Sends a concise usage guide. Does not invoke the agent.

    Args:
        update: The incoming Telegram update.
        context: PTB handler context (unused, present for handler signature).
    """
    help_text = (
        "🛠 Voxnix Help\n\n"
        "Container management:\n"
        "• Create: spin up a container with git and fish\n"
        "• List: show my containers or list workloads\n"
        "• Stop: stop container <name>\n"
        "• Start: start container <name>\n"
        "• Destroy: destroy container <name>\n\n"
        "Commands:\n"
        "/start — welcome message\n"
        "/help — this message\n\n"
        "Just describe what you want in plain language — "
        "I'll figure out the right action."
    )
    if update.effective_message is None:
        raise ValueError("handle_help called on an update with no effective_message")
    await update.effective_message.reply_text(help_text)
