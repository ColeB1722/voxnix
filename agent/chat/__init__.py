"""Voxnix Telegram chat integration layer.

Public API:
  build_application  — construct a fully-wired PTB Application
  handle_message     — the main message handler (for testing / custom wiring)
  owner_from_update  — extract the owner identity from a Telegram Update
  format_response    — split long responses for Telegram's message limit

Typical usage:
    from agent.chat.bot import build_application
    app = build_application(token)
    app.run_polling()

Imports are lazy (via __getattr__) to avoid a circular import cycle:
  agent.agent → agent.chat.history → agent.chat → agent.chat.handlers → agent.agent

No code outside this package uses ``from agent.chat import X`` today —
callers import from the specific submodule (agent.chat.bot, agent.chat.handlers).
The re-exports here are kept for convenience but deferred until actually accessed.
"""

__all__ = [
    "build_application",
    "format_response",
    "handle_message",
    "owner_from_update",
]


def __getattr__(name: str):
    if name == "build_application":
        from agent.chat.bot import build_application

        return build_application

    if name in ("format_response", "handle_message", "owner_from_update"):
        from agent.chat import handlers

        return getattr(handlers, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
