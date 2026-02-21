"""Tests for the async CLI subprocess runner.

TDD — these tests define the contract for the subprocess wrapper
that all agent tools use to invoke Nix/systemd CLI commands.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.cli import CommandResult, run_command


class TestCommandResult:
    """CommandResult is the structured return type for all CLI invocations."""

    def test_success_check(self):
        result = CommandResult(stdout="ok", stderr="", returncode=0)
        assert result.success is True

    def test_failure_check(self):
        result = CommandResult(stdout="", stderr="error", returncode=1)
        assert result.success is False

    def test_nonzero_is_failure(self):
        result = CommandResult(stdout="partial", stderr="warn", returncode=2)
        assert result.success is False

    def test_stdout_stripped(self):
        result = CommandResult(stdout="  output\n", stderr="", returncode=0)
        assert result.stdout == "output"

    def test_stderr_stripped(self):
        result = CommandResult(stdout="", stderr="  warning\n", returncode=0)
        assert result.stderr == "warning"


class TestRunCommand:
    """run_command wraps asyncio.create_subprocess_exec with structured results."""

    async def test_successful_command(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0

        with patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_command("echo", "hello")

        assert result.success is True
        assert result.stdout == "hello"
        assert result.returncode == 0

    async def test_failed_command(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"not found\n")
        mock_proc.returncode = 127

        with patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_command("nonexistent")

        assert result.success is False
        assert result.returncode == 127
        assert "not found" in result.stderr

    async def test_timeout_raises(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()  # kill() is synchronous on asyncio.Process
        mock_proc.wait = AsyncMock()  # wait() is async

        with (
            patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            await run_command("sleep", "999", timeout_seconds=1)

    async def test_timeout_kills_process(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()  # kill() is synchronous on asyncio.Process
        mock_proc.wait = AsyncMock()  # wait() is async

        with (
            patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(TimeoutError),
        ):
            await run_command("sleep", "999", timeout_seconds=1)

        mock_proc.kill.assert_called_once()

    async def test_passes_args_correctly(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch(
            "agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await run_command("nix", "eval", ".#lib.availableModules", "--json")

        mock_exec.assert_called_once()
        args = mock_exec.call_args
        assert args[0] == ("nix", "eval", ".#lib.availableModules", "--json")

    async def test_default_timeout(self):
        """Commands should have a default timeout to prevent hangs."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        with patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_command("echo", "test")

        # Should succeed without specifying timeout — default is applied
        assert result.success is True

    async def test_stderr_captured_on_success(self):
        """Some commands write warnings to stderr even on success."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"output\n", b"warning: Git tree is dirty\n")
        mock_proc.returncode = 0

        with patch("agent.tools.cli.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_command("nix", "eval")

        assert result.success is True
        assert result.stdout == "output"
        assert "dirty" in result.stderr
