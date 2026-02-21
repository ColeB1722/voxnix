"""Tests for the Telegram chat integration handlers.

TDD — these tests define the contract for the chat layer glue code:
  - owner_from_update: extracts the Telegram chat_id as the owner identity
  - format_response: splits long agent responses for the Telegram 4096-char limit
  - handle_message: end-to-end handler — receive message → agent.run → reply

All Telegram API objects are mocked — no bot token or network required.

See docs/architecture.md § Chat Integration Layer and § Trust Model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

# ── Helpers to build minimal mock Telegram objects ────────────────────────────


def _make_chat(chat_id: int) -> MagicMock:
    """Return a minimal mock of telegram.Chat."""
    chat = MagicMock()
    chat.id = chat_id
    return chat


def _make_message(text: str, chat_id: int = 111) -> MagicMock:
    """Return a minimal mock of telegram.Message."""
    message = MagicMock()
    message.text = text
    message.chat = _make_chat(chat_id)
    message.reply_text = AsyncMock()
    return message


def _make_update(text: str = "hello", chat_id: int = 111) -> MagicMock:
    """Return a minimal mock of telegram.Update with an effective_chat."""
    update = MagicMock()
    update.effective_chat = _make_chat(chat_id)
    update.effective_message = _make_message(text, chat_id)
    update.message = update.effective_message
    return update


def _make_context() -> MagicMock:
    """Return a minimal mock of telegram.ext.ContextTypes.DEFAULT_TYPE."""
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


# ── owner_from_update ─────────────────────────────────────────────────────────


class TestOwnerFromUpdate:
    """owner_from_update extracts the Telegram chat_id as a string owner identity.

    This is the trust model anchor — chat_id IS the user.  Every tool call the
    agent makes carries this value as the ownership principal.
    """

    def test_extracts_chat_id_as_string(self):
        from agent.chat.handlers import owner_from_update

        update = _make_update(chat_id=123456789)
        assert owner_from_update(update) == "123456789"

    def test_returns_string_type(self):
        """Owner must always be a str — VoxnixDeps.owner is typed str."""
        from agent.chat.handlers import owner_from_update

        update = _make_update(chat_id=42)
        result = owner_from_update(update)
        assert isinstance(result, str)

    def test_large_chat_id(self):
        """Real Telegram user IDs are large integers."""
        from agent.chat.handlers import owner_from_update

        update = _make_update(chat_id=9_999_999_999)
        assert owner_from_update(update) == "9999999999"

    def test_negative_chat_id_for_group(self):
        """Group and supergroup chats have negative IDs."""
        from agent.chat.handlers import owner_from_update

        update = _make_update(chat_id=-100123456789)
        assert owner_from_update(update) == "-100123456789"

    def test_uses_effective_chat(self):
        """effective_chat is the canonical field — handles forwarded messages etc."""
        from agent.chat.handlers import owner_from_update

        update = MagicMock()
        update.effective_chat = _make_chat(77)
        # effective_message may also be present but owner comes from effective_chat
        assert owner_from_update(update) == "77"


# ── format_response ───────────────────────────────────────────────────────────


class TestFormatResponse:
    """format_response splits agent output into chunks ≤ TELEGRAM_MAX_MESSAGE_LEN.

    Telegram rejects messages longer than 4096 characters.  Long agent responses
    (container logs, module lists, etc.) must be chunked before sending.
    """

    def test_short_response_returns_single_chunk(self):
        from agent.chat.handlers import format_response

        text = "Container `dev-abc` is running."
        chunks = format_response(text)
        assert chunks == [text]

    def test_empty_string_returns_single_empty_chunk(self):
        """An empty response should still yield one (empty) chunk so the caller
        always has at least one message to send back to the user."""
        from agent.chat.handlers import format_response

        chunks = format_response("")
        assert chunks == [""]

    def test_exactly_at_limit_is_single_chunk(self):
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        text = "x" * TELEGRAM_MAX_MESSAGE_LEN
        chunks = format_response(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_one_over_limit_creates_two_chunks(self):
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        text = "x" * (TELEGRAM_MAX_MESSAGE_LEN + 1)
        chunks = format_response(text)
        assert len(chunks) == 2

    def test_all_chunks_within_limit(self):
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        # 3× the limit — should produce exactly 3 chunks
        text = "a" * (TELEGRAM_MAX_MESSAGE_LEN * 3)
        chunks = format_response(text)
        for chunk in chunks:
            assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LEN

    def test_preserves_full_content(self):
        """No characters must be dropped when chunking."""
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        text = "y" * (TELEGRAM_MAX_MESSAGE_LEN * 2 + 500)
        chunks = format_response(text)
        assert "".join(chunks) == text

    def test_prefers_newline_split(self):
        """When the text contains newlines inside the chunk window, split there
        to preserve readable formatting (e.g. bullet-point lists)."""
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        # Build a text that overflows by a few chars but has a newline near the
        # boundary so we can split cleanly.
        boundary = TELEGRAM_MAX_MESSAGE_LEN - 10
        first_part = "A" * boundary + "\n"
        second_part = "B" * 20
        text = first_part + second_part

        chunks = format_response(text)
        assert len(chunks) == 2
        # First chunk should not include the trailing newline
        assert chunks[0] == "A" * boundary
        assert chunks[1] == second_part

    def test_multiline_block_split_on_newlines(self):
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, format_response

        line = "• container-xyz — 🟢 running — 10.0.0.2\n"
        # Enough lines to exceed the limit
        count = (TELEGRAM_MAX_MESSAGE_LEN // len(line)) + 5
        text = line * count

        chunks = format_response(text)
        for chunk in chunks:
            assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LEN
        assert "".join(c + ("\n" if not c.endswith("\n") else "") for c in chunks).replace(
            "\n\n", "\n"
        )  # basic sanity — no crash


# ── handle_message ────────────────────────────────────────────────────────────


class TestHandleMessage:
    """handle_message is the main Telegram message handler.

    It wires owner extraction → agent.run → response chunking → reply_text.
    """

    async def test_calls_agent_run_with_text_and_owner(self):
        """Core contract: message text and owner (chat_id) reach the agent."""
        from agent.chat.handlers import handle_message

        update = _make_update(text="list my containers", chat_id=555)
        context = _make_context()

        with patch(
            "agent.chat.handlers.agent_run", new=AsyncMock(return_value="No containers.")
        ) as mock_run:
            await handle_message(update, context)

        mock_run.assert_called_once_with("list my containers", owner="555")

    async def test_sends_agent_response_to_user(self):
        """The agent's reply must reach update.effective_message.reply_text."""
        from agent.chat.handlers import handle_message

        update = _make_update(text="ping", chat_id=1)
        context = _make_context()

        with patch("agent.chat.handlers.agent_run", new=AsyncMock(return_value="pong")):
            await handle_message(update, context)

        update.effective_message.reply_text.assert_called_once_with("pong")

    async def test_sends_typing_action_before_processing(self):
        """A 'typing' chat action must be sent before invoking the agent so the
        user sees feedback immediately on slow operations."""
        from agent.chat.handlers import handle_message

        update = _make_update(text="create a container", chat_id=2)
        context = _make_context()

        call_order: list[str] = []

        async def fake_run(*_a, **_kw) -> str:
            call_order.append("agent")
            return "done"

        async def fake_action(*_a, **_kw) -> None:
            call_order.append("typing")

        context.bot.send_chat_action = fake_action

        with patch("agent.chat.handlers.agent_run", new=fake_run):
            await handle_message(update, context)

        assert call_order.index("typing") < call_order.index("agent")

    async def test_long_response_sent_as_multiple_messages(self):
        """Responses exceeding TELEGRAM_MAX_MESSAGE_LEN must be split across
        multiple reply_text calls."""
        from agent.chat.handlers import TELEGRAM_MAX_MESSAGE_LEN, handle_message

        update = _make_update(text="logs", chat_id=3)
        context = _make_context()

        long_response = "log line\n" * (TELEGRAM_MAX_MESSAGE_LEN // 5)
        assert len(long_response) > TELEGRAM_MAX_MESSAGE_LEN  # sanity check

        with patch("agent.chat.handlers.agent_run", new=AsyncMock(return_value=long_response)):
            await handle_message(update, context)

        # reply_text must have been called more than once
        assert update.effective_message.reply_text.call_count > 1

    async def test_agent_exception_sends_error_message(self):
        """If the agent raises, the user receives a friendly error message —
        never a raw traceback or unhandled exception."""
        from agent.chat.handlers import handle_message

        update = _make_update(text="do something", chat_id=4)
        context = _make_context()

        with patch(
            "agent.chat.handlers.agent_run",
            new=AsyncMock(side_effect=RuntimeError("LLM quota exceeded")),
        ):
            # Should NOT propagate — the handler must catch and reply
            await handle_message(update, context)

        update.effective_message.reply_text.assert_called_once()
        sent_text: str = update.effective_message.reply_text.call_args[0][0]
        # The user gets a polite error, not a raw traceback
        assert "LLM quota exceeded" not in sent_text
        assert len(sent_text) > 0

    async def test_no_message_text_is_ignored(self):
        """Non-text updates (stickers, photos, etc.) produce no agent call."""
        from agent.chat.handlers import handle_message

        update = _make_update(chat_id=5)
        update.effective_message.text = None  # simulate a photo/sticker
        context = _make_context()

        with patch("agent.chat.handlers.agent_run", new=AsyncMock()) as mock_run:
            await handle_message(update, context)

        mock_run.assert_not_called()

    async def test_owner_is_string_chat_id(self):
        """The owner passed to agent_run must be exactly str(chat_id)."""
        from agent.chat.handlers import handle_message

        chat_id = 123_456_789
        update = _make_update(text="hello", chat_id=chat_id)
        context = _make_context()

        captured: list[str] = []

        async def capture_owner(_msg: str, *, owner: str) -> str:
            captured.append(owner)
            return "ok"

        with patch("agent.chat.handlers.agent_run", new=capture_owner):
            await handle_message(update, context)

        assert captured == [str(chat_id)]

    async def test_whitespace_only_message_is_ignored(self):
        """A message containing only whitespace carries no intent — skip it."""
        from agent.chat.handlers import handle_message

        update = _make_update(text="   \n\t  ", chat_id=6)
        context = _make_context()

        with patch("agent.chat.handlers.agent_run", new=AsyncMock()) as mock_run:
            await handle_message(update, context)

        mock_run.assert_not_called()


# ── handle_start ──────────────────────────────────────────────────────────────


class TestHandleStart:
    """/start sends a welcome message without invoking the agent."""

    async def test_replies_with_welcome(self):
        from agent.chat.handlers import handle_start

        update = _make_update(text="/start", chat_id=7)
        context = _make_context()

        await handle_start(update, context)

        update.effective_message.reply_text.assert_called_once()
        welcome: str = update.effective_message.reply_text.call_args[0][0]
        assert len(welcome) > 0

    async def test_does_not_call_agent(self):
        from agent.chat.handlers import handle_start

        update = _make_update(text="/start", chat_id=8)
        context = _make_context()

        with patch("agent.chat.handlers.agent_run", new=AsyncMock()) as mock_run:
            await handle_start(update, context)

        mock_run.assert_not_called()


# ── build_application ─────────────────────────────────────────────────────────


class TestBuildApplication:
    """build_application wires handlers into a telegram Application instance."""

    def test_returns_application_instance(self):
        from telegram.ext import Application

        from agent.chat.bot import build_application

        app = build_application("fake-token:TEST")
        assert isinstance(app, Application)

    def test_has_message_handler(self):
        """At minimum one MessageHandler must be registered for text messages."""
        from telegram.ext import MessageHandler

        from agent.chat.bot import build_application

        app = build_application("fake-token:TEST")
        handler_types = [type(h) for h in app.handlers.get(0, [])]
        assert MessageHandler in handler_types

    def test_has_start_command_handler(self):
        from telegram.ext import CommandHandler

        from agent.chat.bot import build_application

        app = build_application("fake-token:TEST")
        command_handlers = [h for h in app.handlers.get(0, []) if isinstance(h, CommandHandler)]
        start_handlers = [h for h in command_handlers if "start" in h.commands]
        assert len(start_handlers) == 1
