"""Voxnix Telegram chat integration layer.

Public API:
  build_application  — construct a fully-wired PTB Application
  handle_message     — the main message handler (for testing / custom wiring)
  owner_from_update  — extract the owner identity from a Telegram Update
  format_response    — split long responses for Telegram's message limit

Typical usage:
    from agent.chat import build_application
    app = build_application(token)
    app.run_polling()
"""

from agent.chat.bot import build_application
from agent.chat.handlers import format_response, handle_message, owner_from_update

__all__ = [
    "build_application",
    "format_response",
    "handle_message",
    "owner_from_update",
]
