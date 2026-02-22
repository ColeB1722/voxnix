"""Entry point for the voxnix Telegram bot.

Starts the bot using long polling. Intended to be run as a module:

    python -m agent.chat

or via the justfile:

    just bot

The bot token and all other configuration are read from environment variables
(injected by agenix at runtime, or from a .env file in development).

Logfire tracing is configured here so all agent runs during the bot's lifetime
are captured under a single process.

See docs/architecture.md § Chat Integration Layer and § Implementation.
"""

from __future__ import annotations

import logging

import logfire
from telegram import Update

from agent.chat.bot import build_application
from agent.config import get_settings

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Entrypoint ────────────────────────────────────────────────────────────────


def main() -> None:
    """Start the voxnix Telegram bot with long polling.

    Reads configuration from VoxnixSettings (env vars / .env file).
    Configures Logfire tracing, then runs the bot until interrupted (Ctrl-C
    or SIGTERM from systemd).
    """
    settings = get_settings()

    # Configure Logfire — token is optional; if unset it runs in local/dev mode.
    logfire_token = settings.logfire_token
    logfire.configure(
        token=logfire_token.get_secret_value() if logfire_token else None,
        service_name="voxnix-bot",
    )

    token = settings.telegram_bot_token.get_secret_value()
    logger.info("Starting voxnix bot (model: %s)", settings.llm_model_string)

    application = build_application(token)

    # run_polling blocks until the process receives SIGINT / SIGTERM.
    # allowed_updates=Update.ALL_TYPES ensures we receive all update types
    # (messages, edited messages, etc.) — handlers filter what they care about.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # ignore messages queued while bot was offline
    )


if __name__ == "__main__":
    main()
