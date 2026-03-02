"""Telegram bot application factory for the voxnix agent.

This module provides build_application() — the single function responsible for
constructing a fully-wired python-telegram-bot Application instance.

Responsibilities:
  - Accept a bot token and return a ready-to-run Application
  - Register the access control gate handler (group -1, blocks unauthorized users)
  - Register all message and command handlers (group 0)

Usage (from __main__.py):
    from agent.chat.bot import build_application
    from agent.config import get_settings

    app = build_application(get_settings().telegram_bot_token.get_secret_value())
    app.run_polling(allowed_updates=Update.ALL_TYPES)

Access control (#61):
  The check_access gate handler runs in handler group -1, which PTB processes
  before group 0 (where /start, /help, and the agent dispatch live). If the
  user's chat_id is not in TELEGRAM_ALLOWED_CHAT_IDS, the gate raises
  ApplicationHandlerStop and no downstream handler ever fires.

  As belt-and-suspenders, the catch-all agent dispatch handler also carries a
  filters.User allowlist when configured. This is defense-in-depth — even if
  the gate handler is accidentally removed or bypassed, the agent dispatch
  handler independently rejects unauthorized users.

See docs/architecture.md § Chat Integration Layer and § Trust Model.
"""

from __future__ import annotations

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from agent.chat.handlers import check_access, handle_help, handle_message, handle_start
from agent.config import get_settings


def build_application(token: str) -> Application:
    """Build and return a configured Telegram Application.

    Registers:
      - Group -1: check_access gate (access control — blocks unauthorized users)
      - Group  0: /start → handle_start  (welcome message)
      - Group  0: /help  → handle_help   (usage guide)
      - Group  0: Text messages (non-command) → handle_message (agent dispatch)

    Command handlers are registered before the catch-all message handler so
    PTB's handler priority (group 0, first match) routes /start and /help
    correctly without them reaching handle_message.

    The access control gate in group -1 runs before everything in group 0.
    Unauthorized users never reach any command or message handler.

    Args:
        token: Telegram Bot API token (from VoxnixSettings.telegram_bot_token).

    Returns:
        A fully configured Application ready for run_polling() or run_webhook().
    """
    application: Application = ApplicationBuilder().token(token).build()

    # ── Access control gate (group -1) ─────────────────────────────────────
    #
    # TypeHandler with no specific type filter matches ALL update types.
    # Registered in group -1 so it runs before group 0 handlers.
    # check_access raises ApplicationHandlerStop for unauthorized chat_ids.
    from telegram import Update

    application.add_handler(TypeHandler(Update, check_access), group=-1)

    # ── Command handlers (group 0) ─────────────────────────────────────────
    #
    # Registered first so they take priority over the catch-all text handler.
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("help", handle_help))

    # ── Agent dispatch (group 0) ───────────────────────────────────────────
    #
    # Catch-all text handler — dispatches to the agent for all non-command
    # text messages.  filters.TEXT includes any text message; ~filters.COMMAND
    # excludes messages that start with "/" so commands don't double-fire.
    #
    # Belt-and-suspenders: if an allowlist is configured, also apply
    # filters.User so PTB itself rejects unauthorized users at the filter
    # level — defense-in-depth independent of the gate handler.
    text_filter = filters.TEXT & ~filters.COMMAND

    settings = get_settings()
    allowed_ids = settings.telegram_allowed_chat_ids
    if allowed_ids:
        text_filter = text_filter & filters.User(user_id=list(allowed_ids))

    application.add_handler(MessageHandler(text_filter, handle_message))

    return application
