"""Telegram bot application factory for the voxnix agent.

This module provides build_application() — the single function responsible for
constructing a fully-wired python-telegram-bot Application instance.

Responsibilities:
  - Accept a bot token and return a ready-to-run Application
  - Register all message and command handlers
  - Keep configuration concerns out of the handler layer

Usage (from __main__.py):
    from agent.chat.bot import build_application
    from agent.config import get_settings

    app = build_application(get_settings().telegram_bot_token.get_secret_value())
    app.run_polling(allowed_updates=Update.ALL_TYPES)

See docs/architecture.md § Chat Integration Layer.
"""

from __future__ import annotations

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from agent.chat.handlers import handle_help, handle_message, handle_start


def build_application(token: str) -> Application:
    """Build and return a configured Telegram Application.

    Registers:
      - /start  → handle_start  (welcome message)
      - /help   → handle_help   (usage guide)
      - Text messages (non-command) → handle_message (agent dispatch)

    Command handlers are registered before the catch-all message handler so
    PTB's handler priority (group 0, first match) routes /start and /help
    correctly without them reaching handle_message.

    Args:
        token: Telegram Bot API token (from VoxnixSettings.telegram_bot_token).

    Returns:
        A fully configured Application ready for run_polling() or run_webhook().
    """
    application: Application = ApplicationBuilder().token(token).build()

    # Command handlers — registered first so they take priority over the
    # catch-all text handler below.
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("help", handle_help))

    # Catch-all text handler — dispatches to the agent for all non-command
    # text messages.  Filters.TEXT includes any text message; ~Filters.COMMAND
    # excludes messages that start with "/" so commands don't double-fire.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return application
