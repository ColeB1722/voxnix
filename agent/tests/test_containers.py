"""Tests for container lifecycle tools.

TDD — these tests define the contract for container management operations.
All CLI calls are mocked; no real NixOS host is required to run these tests.

ZFS dataset operations (create_container_dataset, destroy_container_dataset)
are mocked at the agent.tools.containers module level — the ZFS tools have
their own dedicated tests in test_zfs.py.
"""

import logging
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
from agent.tools.zfs import ZfsResult


def _cmd_dispatch(**responses: CommandResult) -> AsyncMock:
    """Return an AsyncMock for run_command that dispatches by the first argument.

    Keys are command names (e.g. "nixos-container", "extra-container").
    Calls with an unrecognised command fall back to ok().

    This avoids ordered side_effect sequences — tests are robust to call-order
    changes within the function under test.

    Example::

        mock_run = _cmd_dispatch(
            **{"nixos-container": fail("not running"), "extra-container": ok()}
        )
    """

    async def _dispatch(*args, **kwargs):
        cmd = args[0] if args else ""
        return responses.get(cmd, ok())

    return AsyncMock(side_effect=_dispatch)


# ── Test fixtures ─────────────────────────────────────────────────────────────

FLAKE_PATH = "/var/lib/voxnix"
OWNER = "chat_123"
CONTAINER_NAME = "test-dev"
MOUNT_PATH = f"/tank/users/{OWNER}/containers/{CONTAINER_NAME}/workspace"
WORKSPACE_DS = f"tank/users/{OWNER}/containers/{CONTAINER_NAME}/workspace"
CONTAINER_DS = f"tank/users/{OWNER}/containers/{CONTAINER_NAME}"

TEST_SPEC = ContainerSpec(
    name=CONTAINER_NAME,
    owner=OWNER,
    modules=["git", "fish"],
)


def ok() -> CommandResult:
    """Successful CLI result."""
    return CommandResult(stdout="", stderr="", returncode=0)


def fail(stderr: str = "error") -> CommandResult:
    """Failed CLI result."""
    return CommandResult(stdout="", stderr=stderr, returncode=1)


def zfs_ok() -> ZfsResult:
    """Successful ZFS dataset result with mount path."""
    return ZfsResult(
        success=True,
        dataset=WORKSPACE_DS,
        message="Created",
        mount_path=MOUNT_PATH,
    )


def zfs_fail(error: str = "zfs error") -> ZfsResult:
    """Failed ZFS dataset result."""
    return ZfsResult(
        success=False,
        dataset=WORKSPACE_DS,
        message="Failed",
        error=error,
    )


def zfs_destroy_ok() -> ZfsResult:
    """Successful ZFS destroy result."""
    return ZfsResult(
        success=True,
        dataset=CONTAINER_DS,
        message="Destroyed",
    )


def zfs_destroy_fail(error: str = "destroy error") -> ZfsResult:
    """Failed ZFS destroy result."""
    return ZfsResult(
        success=False,
        dataset=CONTAINER_DS,
        message="Failed",
        error=error,
    )


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
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
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
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
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
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch("agent.tools.containers.run_command", mock_run),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        args = mock_run.call_args[0]
        assert "--start" in args

    async def test_generator_called_with_spec_and_flake_path(self):
        mock_gen = patch("agent.tools.containers.generate_container_expr", return_value="...")

        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            mock_gen as mock_generate,
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        # The spec should have workspace_path set by the ZFS provisioning step.
        called_spec = mock_generate.call_args[0][0]
        assert called_spec.name == TEST_SPEC.name
        assert called_spec.workspace_path == MOUNT_PATH
        assert mock_generate.call_args[0][1] == FLAKE_PATH

    async def test_build_failure_returns_failure_result(self):
        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("error: build of '/nix/store/...' failed")),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
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
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail(stderr)),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.error == stderr

    async def test_zfs_provisioned_before_container_creation(self):
        """ZFS dataset must be created before the Nix expression is generated."""
        call_order: list[str] = []

        async def mock_zfs(*args, **kwargs):
            call_order.append("zfs_create")
            return zfs_ok()

        def mock_gen(*args, **kwargs):
            call_order.append("nix_gen")
            return "..."

        async def mock_run(*args, **kwargs):
            call_order.append("extra_container")
            return ok()

        with (
            patch("agent.tools.containers.create_container_dataset", mock_zfs),
            patch("agent.tools.containers.generate_container_expr", mock_gen),
            patch("agent.tools.containers.run_command", mock_run),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert call_order == ["zfs_create", "nix_gen", "extra_container"]

    async def test_zfs_failure_aborts_container_creation(self):
        """If ZFS dataset creation fails, container creation does not proceed."""
        mock_run = AsyncMock(return_value=ok())

        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_fail("no space")),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch("agent.tools.containers.run_command", mock_run),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        assert "storage" in result.message.lower() or "provision" in result.message.lower()
        # extra-container should NOT have been called.
        mock_run.assert_not_called()

    async def test_zfs_failure_error_propagated(self):
        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_fail("quota exceeded")),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        assert result.error == "quota exceeded"

    async def test_workspace_path_set_on_spec_before_generation(self):
        """The spec passed to the generator should have workspace_path from ZFS."""
        captured_spec = None

        def capture_gen(spec, flake_path):
            nonlocal captured_spec
            captured_spec = spec
            return "..."

        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", capture_gen),
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
        ):
            await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert captured_spec is not None
        assert captured_spec.workspace_path == MOUNT_PATH

    async def test_build_failure_cleans_up_zfs(self):
        """When extra-container fails with no stdout (build failed), ZFS dataset is destroyed."""
        mock_zfs_destroy = AsyncMock(return_value=zfs_destroy_ok())

        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                # No "Installing containers:" in stdout — pure build failure
                AsyncMock(
                    return_value=CommandResult(stdout="", stderr="build failed", returncode=1)
                ),
            ),
            patch("agent.tools.containers.destroy_container_dataset", mock_zfs_destroy),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        mock_zfs_destroy.assert_called_once_with(OWNER, CONTAINER_NAME)

    async def test_start_failure_preserves_zfs_dataset(self):
        """When install succeeds but start fails, ZFS dataset is NOT destroyed.

        extra-container prints 'Installing containers:' before attempting to start.
        If start fails after install, the container conf is in /etc/nixos-containers/
        and still needs the workspace dataset to exist.
        """
        mock_zfs_destroy = AsyncMock(return_value=zfs_destroy_ok())

        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                # stdout contains "Installing containers:" — install succeeded, start failed
                AsyncMock(
                    return_value=CommandResult(
                        stdout=(
                            "Installing containers:\ndev\n\n"
                            "Starting containers:\ndev\n\n"
                            "Error at extra-container:900"
                        ),
                        stderr="",
                        returncode=1,
                    )
                ),
            ),
            patch("agent.tools.containers.destroy_container_dataset", mock_zfs_destroy),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        # Dataset must NOT be destroyed — container conf is installed and needs it
        mock_zfs_destroy.assert_not_called()

    async def test_build_failure_zfs_cleanup_failure_logged(self, caplog):
        """ZFS cleanup failure after a build failure is logged but doesn't change result."""
        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(
                    return_value=CommandResult(stdout="", stderr="build failed", returncode=1)
                ),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_fail("busy")),
            ),
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        assert any("orphaned ZFS dataset" in r.message for r in caplog.records)

    async def test_heuristic_mismatch_warning_on_nonempty_stdout_without_sentinel(self):
        """When creation fails with non-empty stdout but no 'Installing containers:'
        sentinel, a logfire warning should fire to surface potential heuristic drift.

        This is the observability signal for #81 — if extra-container changes its
        output format, this warning surfaces in traces before it causes data loss.
        """
        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                # Non-empty stdout but no sentinel — heuristic mismatch
                AsyncMock(
                    return_value=CommandResult(
                        stdout="some unexpected output from extra-container",
                        stderr="",
                        returncode=1,
                    )
                ),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
            patch("agent.tools.containers.logfire") as mock_logfire,
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        # Verify the heuristic mismatch warning was emitted
        warning_calls = [
            call
            for call in mock_logfire.warning.call_args_list
            if "heuristic mismatch" in str(call)
        ]
        assert len(warning_calls) >= 1, "Expected a logfire warning about heuristic mismatch"

    async def test_no_heuristic_mismatch_warning_on_empty_stdout(self):
        """When creation fails with empty stdout, no heuristic mismatch warning fires.

        Empty stdout means the build failed before producing any output — that's
        not a heuristic drift scenario, it's a straightforward build failure.
        """
        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(
                    return_value=CommandResult(stdout="", stderr="build failed", returncode=1)
                ),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
            patch("agent.tools.containers.logfire") as mock_logfire,
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        # No heuristic mismatch warning should fire
        warning_calls = [
            call
            for call in mock_logfire.warning.call_args_list
            if "heuristic mismatch" in str(call)
        ]
        assert len(warning_calls) == 0, "Should not warn about heuristic mismatch on empty stdout"

    async def test_no_heuristic_mismatch_warning_when_sentinel_present(self):
        """When the sentinel IS present (install succeeded, start failed),
        no heuristic mismatch warning should fire — the heuristic is working.
        """
        with (
            patch(
                "agent.tools.containers.create_container_dataset",
                AsyncMock(return_value=zfs_ok()),
            ),
            patch("agent.tools.containers.generate_container_expr", return_value="..."),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(
                    return_value=CommandResult(
                        stdout="Installing containers:\ndev\nStarting failed",
                        stderr="",
                        returncode=1,
                    )
                ),
            ),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
            patch("agent.tools.containers.logfire") as mock_logfire,
        ):
            result = await create_container(TEST_SPEC, flake_path=FLAKE_PATH)

        assert result.success is False
        warning_calls = [
            call
            for call in mock_logfire.warning.call_args_list
            if "heuristic mismatch" in str(call)
        ]
        assert len(warning_calls) == 0, "Should not warn when sentinel is present"


# ── destroy_container ─────────────────────────────────────────────────────────


class TestDestroyContainer:
    async def test_success(self):
        with (
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        assert result.success is True
        assert result.name == "test-dev"

    async def test_calls_extra_container_destroy(self):
        mock_run = AsyncMock(return_value=ok())

        with (
            patch("agent.tools.containers.run_command", mock_run),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            await destroy_container("test-dev", owner=OWNER)

        args = mock_run.call_args[0]
        assert "extra-container" in args
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

    async def test_failure_logs_to_logger(self, caplog):
        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("destroy error")),
            ),
        ):
            await destroy_container("test-dev")

        assert any("destroy_container failed" in r.message for r in caplog.records)
        assert any("test-dev" in r.message for r in caplog.records)

    async def test_success_message_includes_name(self):
        with (
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        assert "test-dev" in result.message

    async def test_zfs_cleanup_called_with_owner(self):
        """When owner is provided, ZFS dataset is cleaned up after container destruction."""
        mock_zfs_destroy = AsyncMock(return_value=zfs_destroy_ok())

        with (
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch("agent.tools.containers.destroy_container_dataset", mock_zfs_destroy),
        ):
            await destroy_container("test-dev", owner=OWNER)

        mock_zfs_destroy.assert_called_once_with(OWNER, "test-dev")

    async def test_no_zfs_cleanup_without_owner(self):
        """When owner is None, ZFS dataset is left intact."""
        mock_zfs_destroy = AsyncMock(return_value=zfs_destroy_ok())

        with (
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch("agent.tools.containers.destroy_container_dataset", mock_zfs_destroy),
        ):
            await destroy_container("test-dev")

        mock_zfs_destroy.assert_not_called()

    async def test_zfs_cleanup_failure_still_reports_container_destroyed(self):
        """Container destruction succeeded — ZFS failure is noted but success is True."""
        with (
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_fail("busy")),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        assert result.success is True
        assert "storage cleanup failed" in result.message.lower()

    async def test_zfs_cleanup_failure_logs_error(self, caplog):
        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_fail("dataset is busy")),
            ),
        ):
            await destroy_container("test-dev", owner=OWNER)

        assert any("ZFS cleanup failed" in r.message for r in caplog.records)

    async def test_container_failure_skips_zfs_cleanup(self):
        """If the container itself can't be destroyed, don't try ZFS cleanup."""
        mock_zfs_destroy = AsyncMock(return_value=zfs_destroy_ok())

        with (
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("container busy")),
            ),
            patch("agent.tools.containers.destroy_container_dataset", mock_zfs_destroy),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        assert result.success is False
        mock_zfs_destroy.assert_not_called()

    async def test_tailscale_logout_called_before_destroy(self):
        """tailscale logout is attempted inside the container before extra-container destroy."""
        mock_run = _cmd_dispatch(**{"nixos-container": ok(), "extra-container": ok()})

        with (
            patch("agent.tools.containers.run_command", mock_run),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        assert result.success is True

        # Verify logout was attempted via nixos-container run ... tailscale logout
        all_calls = mock_run.call_args_list
        logout_calls = [
            c for c in all_calls if c.args[0] == "nixos-container" and "logout" in c.args
        ]
        assert len(logout_calls) == 1, "Expected exactly one tailscale logout call"
        assert "run" in logout_calls[0].args
        assert "test-dev" in logout_calls[0].args
        assert "tailscale" in logout_calls[0].args

    async def test_tailscale_logout_failure_does_not_abort_destroy(self):
        """If tailscale logout fails (e.g. container stopped), destroy still proceeds."""
        mock_run = _cmd_dispatch(
            **{
                "nixos-container": fail("Container 'test-dev' is not running"),
                "extra-container": ok(),
            }
        )

        with (
            patch("agent.tools.containers.run_command", mock_run),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        # Destroy succeeded despite the logout failure
        assert result.success is True

        # extra-container destroy was still called
        destroy_calls = [
            c
            for c in mock_run.call_args_list
            if c.args[0] == "extra-container" and "destroy" in c.args
        ]
        assert len(destroy_calls) == 1

    async def test_tailscale_logout_failure_not_logged_as_error(self, caplog):
        """Tailscale logout failure is a debug-level event, not an error."""
        mock_run = _cmd_dispatch(
            **{"nixos-container": fail("not enrolled"), "extra-container": ok()}
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch("agent.tools.containers.run_command", mock_run),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            await destroy_container("test-dev", owner=OWNER)

        # No ERROR-level log for a best-effort logout that fails
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        logout_errors = [r for r in error_records if "logout" in r.message.lower()]
        assert len(logout_errors) == 0, (
            f"Expected no error logs for tailscale logout failure, got: {logout_errors}"
        )

    async def test_tailscale_logout_exception_does_not_abort_destroy(self):
        """If run_command raises (OSError, timeout, etc.), destroy still completes."""
        call_count = 0

        async def _raise_then_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[0] == "nixos-container":
                raise OSError("nixos-container: command not found")
            return ok()

        with (
            patch("agent.tools.containers.run_command", side_effect=_raise_then_ok),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            result = await destroy_container("test-dev", owner=OWNER)

        # Destroy must succeed even though logout raised
        assert result.success is True

    async def test_tailscale_logout_exception_logged_at_debug(self, caplog):
        """An exception in _tailscale_logout is logged at debug, not error."""

        async def _raise_then_ok(*args, **kwargs):
            if args[0] == "nixos-container":
                raise OSError("unexpected")
            return ok()

        with (
            caplog.at_level(logging.DEBUG, logger="agent.tools.containers"),
            patch("agent.tools.containers.run_command", side_effect=_raise_then_ok),
            patch(
                "agent.tools.containers.destroy_container_dataset",
                AsyncMock(return_value=zfs_destroy_ok()),
            ),
        ):
            await destroy_container("test-dev", owner=OWNER)

        # Exception must be captured at debug level, not error
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        logout_errors = [r for r in error_records if "logout" in r.message.lower()]
        assert len(logout_errors) == 0, f"Expected no error-level logout logs, got: {logout_errors}"
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("logout" in r.message.lower() for r in debug_records), (
            "Expected a debug-level log for the swallowed logout exception"
        )


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

    async def test_failure_logs_to_logger(self, caplog):
        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("start error")),
            ),
        ):
            await start_container("test-dev")

        assert any("start_container failed" in r.message for r in caplog.records)
        assert any("test-dev" in r.message for r in caplog.records)

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

    async def test_failure_logs_to_logger(self, caplog):
        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.containers"),
            patch(
                "agent.tools.containers.run_command",
                AsyncMock(return_value=fail("stop error")),
            ),
        ):
            await stop_container("test-dev")

        assert any("stop_container failed" in r.message for r in caplog.records)
        assert any("test-dev" in r.message for r in caplog.records)

    async def test_success_message_includes_name(self):
        with patch("agent.tools.containers.run_command", AsyncMock(return_value=ok())):
            result = await stop_container("test-dev")

        assert "test-dev" in result.message
