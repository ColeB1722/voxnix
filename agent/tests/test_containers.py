"""Tests for container lifecycle tools.

TDD — these tests define the contract for container management operations.
All CLI calls are mocked; no real NixOS host is required to run these tests.
"""

from unittest.mock import AsyncMock, patch

from agent.nix_gen.models import ContainerSpec
from agent.tools.cli import CommandResult
from agent.tools.containers import (
    ContainerResult,
    create_container,
    destroy_container,
    start_container,
    stop_container,
)

# ── Test fixtures ─────────────────────────────────────────────────────────────

FLAKE_PATH = "/var/lib/voxnix"

TEST_SPEC = ContainerSpec(
    name="test-dev",
    owner="chat_123",
    modules=["git", "fish"],
)


def ok() -> CommandResult:
    """Successful CLI result."""
    return CommandResult(stdout="", stderr="", returncode=0)


def fail(stderr: str = "error") -> CommandResult:
    """Failed CLI result."""
    return CommandResult(stdout="", stderr=stderr, returncode=1)


# ── ContainerResult ───────────────────────────────────────────────────────────


class TestContainerResult:
    def test_success_result(self):
        result = ContainerResult(success=True, name="test-dev", message="Container started")
        assert result.success is True
        assert result.name == "test-dev"

    def test_failure_result(self):
        result = ContainerResult(
            success=False, name="test-dev", message="Failed", error="Build error"
        )
        assert result.success is False
        assert result.error == "Build error"

    def test_error_defaults_to_none(self):
        result = ContainerResult(success=True, name="test-dev", message="ok")
        assert result.error is None


# ── create_container ──────────────────────────────────────────────────────────


class TestCreateContainer:
    async def test_success(self):
        with (
            patch(
                "agent.tools.containers.generate_container_expr",
                return_value="{ containers.test-dev = {}; }",
            ),
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is True
        assert result.name == TEST_SPEC.name

    async def test_calls_extra_container_create(self):
        mock_run = AsyncMock(return_value=ok())

        with (
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch("agent.tools.containers.run_command", mock_run),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        args = mock_run.call_args[0]
        assert args[0] == "extra-container"
        assert "create" in args

    async def test_passes_start_flag(self):
        mock_run = AsyncMock(return_value=ok())

        with (
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch("agent.tools.containers.run_command", mock_run),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        args = mock_run.call_args[0]
        assert "--start" in args

    async def test_generator_called_with_spec_and_flake_path(self):
        mock_gen = patch("agent.tools.containers.generate_container_expr", return_value="...")

        with (
            mock_gen as mock_generate,
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        mock_generate.assert_called_once_with(TEST_SPEC, FLAKE_PATH)

    async def test_build_failure_returns_failure_result(self):
        with (
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("error: build of '/nix/store/...' failed")),
            ),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        assert result.name == TEST_SPEC.name
        assert result.error is not None
        assert "build" in result.error

    async def test_error_message_on_failure(self):
        stderr = "error: attribute 'unknown-module' missing"

        with (
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail(stderr)),
            ),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.error == stderr


# ── destroy_container ─────────────────────────────────────────────────────────


class TestDestroyContainer:
    async def test_success(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await destroy_container("test-dev")

        assert result.success is True
        assert result.name == "test-dev"

    async def test_calls_nixos_container_destroy(self):
        mock_run = AsyncMock(return_value=ok())

        with patch("agent.tools.containers.run_command", mock_run):
            await destroy_container("test-dev")

        args = mock_run.call_args[0]
        assert "nixos-container" in args
        assert "destroy" in args
        assert "test-dev" in args

    async def test_failure_container_not_found(self):
        with patch(
            "agent.tools.containers.run_command",
            AsyncMock(return_value=fail("Machine 'test-dev' not known")),
        ):
            result = await destroy_container("test-dev")

        assert result.success is False
        assert result.error is not None
        assert "not known" in result.error

    async def test_success_message_includes_name(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await destroy_container("test-dev")

        assert "test-dev" in result.message


# ── start_container ───────────────────────────────────────────────────────────


class TestStartContainer:
    async def test_success(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await start_container("test-dev")

        assert result.success is True
        assert result.name == "test-dev"

    async def test_calls_nixos_container_start(self):
        mock_run = AsyncMock(return_value=ok())

        with patch("agent.tools.containers.run_command", mock_run):
            await start_container("test-dev")

        args = mock_run.call_args[0]
        assert "nixos-container" in args
        assert "start" in args
        assert "test-dev" in args

    async def test_failure(self):
        with patch(
            "agent.tools.containers.run_command",
            AsyncMock(return_value=fail("Failed to start container")),
        ):
            result = await start_container("test-dev")

        assert result.success is False
        assert result.error is not None

    async def test_already_running_is_failure(self):
        """Starting an already-running container should surface the error."""
        with patch(
            "agent.tools.containers.run_command",
            AsyncMock(return_value=fail("Container already running")),
        ):
            result = await start_container("test-dev")

        assert result.success is False


# ── stop_container ────────────────────────────────────────────────────────────


class TestStopContainer:
    async def test_success(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await stop_container("test-dev")

        assert result.success is True
        assert result.name == "test-dev"

    async def test_calls_nixos_container_stop(self):
        mock_run = AsyncMock(return_value=ok())

        with patch("agent.tools.containers.run_command", mock_run):
            await stop_container("test-dev")

        args = mock_run.call_args[0]
        assert "nixos-container" in args
        assert "stop" in args
        assert "test-dev" in args

    async def test_failure(self):
        with patch(
            "agent.tools.containers.run_command",
            AsyncMock(return_value=fail("Failed to stop container")),
        ):
            result = await stop_container("test-dev")

        assert result.success is False
        assert result.error is not None

    async def test_success_message_includes_name(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await stop_container("test-dev")

        assert "test-dev" in result.message
