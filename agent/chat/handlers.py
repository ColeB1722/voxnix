"""Telegram message handlers for the voxnix agent chat layer.

Responsibilities:
  - Extract the Telegram chat_id as the owner identity (trust model anchor)
  - Dispatch text messages to the PydanticAI agent
  - Split long agent responses into Telegram-sized chunks
  - Handle errors gracefully — users never see raw tracebacks

Architecture decisions reflected here:
  - chat_id IS the user identity — no separate auth system (see docs/architecture.md § Trust Model)
  - agent.run() is called with owner=str(chat_id), which flows into VoxnixDeps
  - The handler is the boundary between Telegram and the agent — it owns error handling
  - Typing action is sent before the agent runs so the user sees immediate feedback

See docs/architecture.md § Chat Integration Layer and § Trust Model.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.constants import ChatAction

from agent.agent import run as agent_run  # re-exported for easy mocking in tests

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_MESSAGE_LEN: int = 4096


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
    assert update.effective_chat is not None  # guaranteed in message/command handlers
    return str(update.effective_chat.id)


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
    assert update.effective_message is not None
    assert update.effective_chat is not None

    owner = owner_from_update(update)
    chat_id = update.effective_chat.id

    # 2. Send typing indicator before any blocking work.
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 3. Run the agent — catch all exceptions to keep the bot alive.
    try:
        response = await agent_run(text.strip(), owner=owner)
    except Exception:
        logger.exception("Agent run failed for owner=%s", owner)
        await update.effective_message.reply_text(  # asserted non-None above
            "⚠️ Something went wrong processing your request. Please try again."
        )
        return

    # 4. Send the response, splitting at the Telegram message limit if needed.
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
        "👋 *Welcome to Voxnix!*\n\n"
        "I'm your personal NixOS infrastructure orchestrator. "
        "Talk to me in plain language to manage containers on your appliance.\n\n"
        "Try:\n"
        "• _Spin up a dev container with git and fish_\n"
        "• _List my containers_\n"
        "• _Stop container dev\\-abc_\n"
        "• _Destroy container dev\\-abc_\n\n"
        "Type /help for more information."
    )
    assert update.effective_message is not None
    await update.effective_message.reply_text(welcome, parse_mode="MarkdownV2")


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command.

    Sends a concise usage guide. Does not invoke the agent.

    Args:
        update: The incoming Telegram update.
        context: PTB handler context (unused, present for handler signature).
    """
    help_text = (
        "🛠 *Voxnix Help*\n\n"
        "*Container management:*\n"
        "• Create: _spin up a container with git and fish_\n"
        "• List: _show my containers_ or _list workloads_\n"
        "• Stop: _stop container \\<name\\>_\n"
        "• Start: _start container \\<name\\>_\n"
        "• Destroy: _destroy container \\<name\\>_\n\n"
        "*Commands:*\n"
        "/start — welcome message\n"
        "/help — this message\n\n"
        "Just describe what you want in plain language — "
        "I'll figure out the right action."
    )
    assert update.effective_message is not None
    await update.effective_message.reply_text(help_text, parse_mode="MarkdownV2")
