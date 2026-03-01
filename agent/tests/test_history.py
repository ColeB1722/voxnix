"""Tests for the per-chat conversation history store.

TDD — these tests define the contract for ConversationStore:
  - get() returns empty list for unknown or expired chats
  - append() stores messages and they're retrievable via get()
  - TTL expiry discards stale histories on access
  - max_messages cap trims oldest messages when exceeded (memory safety)
  - clear() and clear_all() remove histories explicitly
  - active_chats() counts non-expired entries (with sweep)
  - get() returns a copy — mutations don't affect the store
  - append() with empty list is a no-op

Context window trimming (what the LLM sees) is NOT tested here — that's
the history_processor's job, tested via agent-level tests.

No external dependencies — all ModelMessage objects are mocked.

See #48 (conversation history) and #62 (TTL-based multi-turn conversation).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.chat.history import ConversationStore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_messages(n: int = 2) -> list[MagicMock]:
    """Return a list of n mock ModelMessage objects.

    Each mock has a unique _test_index attribute for identity tracking.
    In real usage these would be ModelRequest and ModelResponse instances,
    but ConversationStore treats them as opaque list items.
    """
    msgs = []
    for i in range(n):
        m = MagicMock()
        m._test_index = i
        msgs.append(m)
    return msgs


def _make_turn() -> list[MagicMock]:
    """Return a pair of mocks representing one conversation turn (request + response)."""
    return _make_messages(2)


# ── Basic get / append ────────────────────────────────────────────────────────


class TestGetAppendBasics:
    """Core contract: append messages, get them back."""

    def test_get_unknown_chat_returns_empty_list(self):
        store = ConversationStore()
        assert store.get("unknown") == []

    def test_append_then_get_returns_messages(self):
        store = ConversationStore()
        msgs = _make_turn()
        store.append("chat1", msgs)
        result = store.get("chat1")
        assert result == msgs

    def test_multiple_appends_accumulate(self):
        store = ConversationStore()
        turn1 = _make_turn()
        turn2 = _make_turn()
        store.append("chat1", turn1)
        store.append("chat1", turn2)
        result = store.get("chat1")
        assert result == turn1 + turn2

    def test_different_chats_are_independent(self):
        store = ConversationStore()
        msgs_a = _make_turn()
        msgs_b = _make_turn()
        store.append("a", msgs_a)
        store.append("b", msgs_b)
        assert store.get("a") == msgs_a
        assert store.get("b") == msgs_b

    def test_append_empty_list_is_noop(self):
        store = ConversationStore()
        store.append("chat1", [])
        assert store.get("chat1") == []

    def test_append_empty_list_preserves_existing(self):
        store = ConversationStore()
        msgs = _make_turn()
        store.append("chat1", msgs)
        store.append("chat1", [])
        assert store.get("chat1") == msgs

    def test_get_returns_copy_not_reference(self):
        """Mutating the returned list must not affect the store."""
        store = ConversationStore()
        msgs = _make_turn()
        store.append("chat1", msgs)
        result = store.get("chat1")
        result.clear()  # mutate the returned list
        assert store.get("chat1") == msgs  # store is unaffected


# ── TTL expiry ────────────────────────────────────────────────────────────────


class TestTTLExpiry:
    """Histories older than ttl_seconds are discarded on access."""

    def test_fresh_history_is_not_expired(self):
        store = ConversationStore(ttl_seconds=60.0)
        msgs = _make_turn()
        store.append("chat1", msgs)
        assert store.get("chat1") == msgs

    def test_expired_history_returns_empty(self):
        store = ConversationStore(ttl_seconds=10.0)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        # 11 seconds later — expired
        with patch("agent.chat.history.time.monotonic", return_value=1011.0):
            assert store.get("chat1") == []

    def test_access_within_ttl_is_fine(self):
        store = ConversationStore(ttl_seconds=10.0)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        # 9 seconds later — still alive
        with patch("agent.chat.history.time.monotonic", return_value=1009.0):
            assert store.get("chat1") == msgs

    def test_get_resets_ttl_timer(self):
        """Accessing history counts as activity — it should reset the TTL."""
        store = ConversationStore(ttl_seconds=10.0)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        # 8 seconds later — get() resets the timer
        with patch("agent.chat.history.time.monotonic", return_value=1008.0):
            store.get("chat1")

        # 8 more seconds from the get() — only 8s since last activity, not expired
        with patch("agent.chat.history.time.monotonic", return_value=1016.0):
            assert store.get("chat1") == msgs

    def test_append_resets_ttl_timer(self):
        """Appending new messages counts as activity — resets the TTL."""
        store = ConversationStore(ttl_seconds=10.0)
        turn1 = _make_turn()
        turn2 = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", turn1)

        # 8 seconds later — append resets the timer
        with patch("agent.chat.history.time.monotonic", return_value=1008.0):
            store.append("chat1", turn2)

        # 8 more seconds from the append — still alive (only 8s since last activity)
        with patch("agent.chat.history.time.monotonic", return_value=1016.0):
            assert store.get("chat1") == turn1 + turn2

    def test_ttl_zero_disables_expiry(self):
        """ttl_seconds=0 means history never expires."""
        store = ConversationStore(ttl_seconds=0)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        # Way in the future — still alive
        with patch("agent.chat.history.time.monotonic", return_value=999999.0):
            assert store.get("chat1") == msgs

    def test_ttl_negative_disables_expiry(self):
        store = ConversationStore(ttl_seconds=-1)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        with patch("agent.chat.history.time.monotonic", return_value=999999.0):
            assert store.get("chat1") == msgs

    def test_expired_entry_is_removed_from_internal_dict(self):
        """After get() discards an expired entry, it should be gone from the store."""
        store = ConversationStore(ttl_seconds=10.0)
        msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", msgs)

        with patch("agent.chat.history.time.monotonic", return_value=1011.0):
            store.get("chat1")  # triggers expiry

        assert "chat1" not in store._chats

    def test_append_to_expired_chat_starts_fresh(self):
        """If a chat's history expired, a new append should start a clean slate."""
        store = ConversationStore(ttl_seconds=10.0)
        old_msgs = _make_turn()
        new_msgs = _make_turn()

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("chat1", old_msgs)

        # Expired
        with patch("agent.chat.history.time.monotonic", return_value=1011.0):
            store.append("chat1", new_msgs)

        with patch("agent.chat.history.time.monotonic", return_value=1012.0):
            result = store.get("chat1")
            # Only new messages — old ones were discarded
            assert result == new_msgs


# ── Memory safety cap ─────────────────────────────────────────────────────────


class TestMemoryCap:
    """Store enforces a hard max_messages cap to prevent unbounded memory growth.

    This is a memory safety concern, NOT context window management.
    Context window trimming is handled by the agent's history_processors.
    """

    def test_within_limit_no_trimming(self):
        store = ConversationStore(max_messages=10)
        msgs = _make_messages(6)
        store.append("chat1", msgs)
        assert store.get("chat1") == msgs

    def test_exceeding_limit_trims_oldest(self):
        store = ConversationStore(max_messages=4)
        turn1 = _make_messages(2)
        turn2 = _make_messages(2)
        turn3 = _make_messages(2)

        store.append("chat1", turn1)
        store.append("chat1", turn2)
        store.append("chat1", turn3)

        result = store.get("chat1")
        # 6 messages total, limit is 4, so turn1 (first 2) should be dropped
        assert len(result) == 4
        assert result == turn2 + turn3

    def test_exactly_at_limit_no_trimming(self):
        store = ConversationStore(max_messages=4)
        msgs = _make_messages(4)
        store.append("chat1", msgs)
        assert store.get("chat1") == msgs

    def test_single_large_append_trimmed(self):
        """A single append that exceeds the limit should still be trimmed."""
        store = ConversationStore(max_messages=4)
        msgs = _make_messages(10)
        store.append("chat1", msgs)
        result = store.get("chat1")
        assert len(result) == 4
        # Should keep the last 4 messages
        assert result == msgs[-4:]

    def test_max_messages_zero_means_unlimited(self):
        store = ConversationStore(max_messages=0)
        msgs = _make_messages(100)
        store.append("chat1", msgs)
        assert len(store.get("chat1")) == 100

    def test_max_messages_negative_means_unlimited(self):
        store = ConversationStore(max_messages=-1)
        msgs = _make_messages(100)
        store.append("chat1", msgs)
        assert len(store.get("chat1")) == 100


# ── clear / clear_all ────────────────────────────────────────────────────────


class TestClear:
    """Explicit history clearing."""

    def test_clear_removes_specific_chat(self):
        store = ConversationStore()
        store.append("a", _make_turn())
        store.append("b", _make_turn())
        store.clear("a")
        assert store.get("a") == []
        assert store.get("b") != []

    def test_clear_nonexistent_chat_is_noop(self):
        store = ConversationStore()
        store.clear("does_not_exist")  # should not raise

    def test_clear_all_removes_everything(self):
        store = ConversationStore()
        store.append("a", _make_turn())
        store.append("b", _make_turn())
        store.append("c", _make_turn())
        store.clear_all()
        assert store.get("a") == []
        assert store.get("b") == []
        assert store.get("c") == []


# ── active_chats ──────────────────────────────────────────────────────────────


class TestActiveChats:
    """active_chats() counts non-expired entries and sweeps expired ones."""

    def test_empty_store_has_zero_active(self):
        store = ConversationStore()
        assert store.active_chats() == 0

    def test_counts_active_chats(self):
        store = ConversationStore()
        store.append("a", _make_turn())
        store.append("b", _make_turn())
        store.append("c", _make_turn())
        assert store.active_chats() == 3

    def test_sweeps_expired_chats(self):
        store = ConversationStore(ttl_seconds=10.0)

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("old", _make_turn())

        with patch("agent.chat.history.time.monotonic", return_value=1008.0):
            store.append("new", _make_turn())

        # 12 seconds after "old" was created — "old" is expired, "new" is not
        with patch("agent.chat.history.time.monotonic", return_value=1012.0):
            assert store.active_chats() == 1

        # "old" should have been swept
        assert "old" not in store._chats

    def test_sweep_with_ttl_disabled(self):
        """When TTL is disabled, nothing is swept."""
        store = ConversationStore(ttl_seconds=0)

        with patch("agent.chat.history.time.monotonic", return_value=1000.0):
            store.append("a", _make_turn())

        with patch("agent.chat.history.time.monotonic", return_value=999999.0):
            assert store.active_chats() == 1


# ── Properties ────────────────────────────────────────────────────────────────


class TestProperties:
    """Verify configuration is exposed correctly."""

    def test_max_messages_property(self):
        store = ConversationStore(max_messages=42)
        assert store.max_messages == 42

    def test_ttl_seconds_property(self):
        store = ConversationStore(ttl_seconds=300.0)
        assert store.ttl_seconds == 300.0

    def test_default_values(self):
        from agent.chat.history import DEFAULT_MAX_STORE_MESSAGES

        store = ConversationStore()
        assert store.max_messages == DEFAULT_MAX_STORE_MESSAGES
        assert store.ttl_seconds == 1800.0
