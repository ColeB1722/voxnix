"""Per-chat conversation history store with TTL and max-turn support.

Manages PydanticAI message histories keyed by Telegram chat_id so the agent
can maintain context across multiple turns within a conversation.

Design decisions:
  - In-memory storage (dict) — simple, no external deps, fine for single-process bot.
    Lost on restart, which is acceptable: infrastructure commands are mostly
    stateless, and stale history from hours ago would confuse more than help.
  - TTL-based expiry — conversations go stale. A message history from 30 minutes
    ago is probably still relevant; one from 6 hours ago is not. Each chat has
    a last-activity timestamp; histories older than the TTL are discarded on access.
  - Max-turn cap — prevents unbounded memory growth and keeps the LLM context
    window manageable. When the cap is exceeded, the oldest turns are dropped.
    A "turn" is one user message + one assistant response (2 ModelMessage objects),
    so max_turns=20 means up to 40 messages in the history list.
  - Thread-safe for asyncio — the bot's event loop is single-threaded, and
    per-chat locks in handlers.py already serialise concurrent messages from
    the same user. No additional locking needed here.

The store is instantiated once in handlers.py and lives for the process lifetime.
It is passed around explicitly (no globals) for testability.

See #48 (conversation history) and #62 (TTL-based multi-turn conversation).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage


@dataclass
class _ChatHistory:
    """Internal state for a single chat's conversation history."""

    messages: list[ModelMessage] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


class ConversationStore:
    """Per-chat conversation history with TTL expiry and turn limits.

    Usage::

        store = ConversationStore(max_turns=20, ttl_seconds=1800)

        # Before agent.run — get existing history (or empty list if expired/new)
        history = store.get(chat_id="12345")

        # After agent.run — append the new messages from this turn
        store.append(chat_id="12345", new_messages=result.new_messages())

    Args:
        max_turns: Maximum number of conversation turns to retain per chat.
                   Each turn is typically 2 messages (user + assistant), so
                   the message list can hold up to ``max_turns * 2`` entries.
                   Set to 0 or negative for unlimited (not recommended).
        ttl_seconds: Seconds of inactivity after which a chat's history is
                     discarded. Resets on every ``get()`` or ``append()`` call.
                     Set to 0 or negative to disable TTL (history never expires).
    """

    def __init__(self, max_turns: int = 20, ttl_seconds: float = 1800.0) -> None:
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds
        self._chats: dict[str, _ChatHistory] = {}

    @property
    def max_turns(self) -> int:
        """Maximum conversation turns retained per chat."""
        return self._max_turns

    @property
    def ttl_seconds(self) -> float:
        """Inactivity timeout in seconds before history is discarded."""
        return self._ttl_seconds

    def get(self, chat_id: str) -> list[ModelMessage]:
        """Return the current message history for a chat.

        If the chat has no history or the history has expired (older than
        ``ttl_seconds`` since last activity), returns an empty list.

        Accessing history counts as activity — it resets the TTL timer.

        Args:
            chat_id: The Telegram chat ID (stringified).

        Returns:
            A *copy* of the stored messages. The caller may pass this to
            ``agent.run(message_history=...)`` without risk of mutation.
        """
        entry = self._chats.get(chat_id)
        if entry is None:
            return []

        if self._is_expired(entry):
            del self._chats[chat_id]
            return []

        # Touch — accessing history resets the inactivity timer.
        entry.last_activity = time.monotonic()
        return list(entry.messages)

    def append(self, chat_id: str, new_messages: Sequence[ModelMessage]) -> None:
        """Append new messages from an agent run to a chat's history.

        Creates the history entry if it doesn't exist. Resets the TTL timer.
        If appending causes the history to exceed ``max_turns``, the oldest
        messages are trimmed (from the front of the list).

        Args:
            chat_id: The Telegram chat ID (stringified).
            new_messages: Messages from ``result.new_messages()`` to store.
                          Typically contains one ``ModelRequest`` (user prompt)
                          and one ``ModelResponse`` (assistant reply), plus any
                          tool call/result pairs in between.
        """
        if not new_messages:
            return

        entry = self._chats.get(chat_id)

        # If expired or new, start fresh.
        if entry is None or self._is_expired(entry):
            entry = _ChatHistory()
            self._chats[chat_id] = entry

        entry.messages.extend(new_messages)
        entry.last_activity = time.monotonic()

        # Enforce the turn limit. Each turn is roughly 2 messages (request +
        # response), but tool calls can add intermediaries. We cap on total
        # message count: max_turns * 2 is a reasonable heuristic.
        if self._max_turns > 0:
            max_messages = self._max_turns * 2
            if len(entry.messages) > max_messages:
                # Drop the oldest messages to get back within the limit.
                entry.messages = entry.messages[-max_messages:]

    def clear(self, chat_id: str) -> None:
        """Remove all conversation history for a specific chat.

        This is useful for a ``/clear`` or ``/reset`` command.

        Args:
            chat_id: The Telegram chat ID (stringified).
        """
        self._chats.pop(chat_id, None)

    def clear_all(self) -> None:
        """Remove all conversation histories. Primarily for testing."""
        self._chats.clear()

    def active_chats(self) -> int:
        """Return the number of chats with non-expired histories.

        Performs a sweep of expired entries as a side effect.
        """
        self._sweep_expired()
        return len(self._chats)

    def _is_expired(self, entry: _ChatHistory) -> bool:
        """Check if a history entry has exceeded the TTL."""
        if self._ttl_seconds <= 0:
            return False
        return (time.monotonic() - entry.last_activity) > self._ttl_seconds

    def _sweep_expired(self) -> None:
        """Remove all expired entries. Called lazily, not on a timer."""
        if self._ttl_seconds <= 0:
            return
        now = time.monotonic()
        expired = [
            cid
            for cid, entry in self._chats.items()
            if (now - entry.last_activity) > self._ttl_seconds
        ]
        for cid in expired:
            del self._chats[cid]
