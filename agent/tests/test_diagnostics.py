"""Tests for the diagnostic tools module.

TDD — these tests define the contract for the agent's self-diagnosis tools:
  - check_host_health: runs a checklist of host-level health indicators
  - get_container_logs: retrieves journal logs for a container
  - get_container_status: gets systemd/machinectl status for a container
  - get_tailscale_status: queries Tailscale status in a container or on host
  - get_service_status: checks host-level systemd service status (allowlisted)

All CLI commands are mocked — no real system access required.

See #47 (diagnostic tools for the agent).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agent.tools.cli import CommandResult
from agent.tools.diagnostics import (
    DEFAULT_LOG_LINES,
    DiagnosticResult,
    check_host_health,
    get_container_logs,
    get_container_status,
    get_service_status,
    get_tailscale_status,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful CommandResult."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=0)


def _fail(stderr: str = "error", stdout: str = "") -> CommandResult:
    """Return a failed CommandResult."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=1)


# ── DiagnosticResult ──────────────────────────────────────────────────────────


class TestDiagnosticResult:
    def test_success_result(self):
        r = DiagnosticResult(success=True, output="all good")
        assert r.success is True
        assert r.output == "all good"
        assert r.error is None

    def test_failure_result(self):
        r = DiagnosticResult(success=False, output="", error="something broke")
        assert r.success is False
        assert r.error == "something broke"


# ── check_host_health ─────────────────────────────────────────────────────────


class TestCheckHostHealth:
    """Host health check runs multiple sub-checks and aggregates results."""

    async def test_all_checks_pass(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="/nix/store/.../extra-container"),  # which extra-container
                _ok(stdout="MACHINE CLASS SERVICE"),  # machinectl list
                _ok(stdout="container@.service static"),  # systemctl list-unit-files
                _ok(stdout="zfs-2.2.0"),  # zfs version
            ]
            result = await check_host_health()

        assert result.success is True
        assert "All checks passed" in result.output
        assert "OK: extra-container found" in result.output
        assert "OK: machinectl is responsive" in result.output
        assert "OK: container@.service template found" in result.output
        assert "OK: ZFS available" in result.output

    async def test_extra_container_missing(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(stderr="not found"),  # which extra-container
                _ok(),  # machinectl list
                _ok(stdout="container@.service static"),
                _ok(stdout="zfs-2.2.0"),
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: extra-container not found" in result.output
        assert "Some checks failed" in result.output

    async def test_machinectl_unresponsive(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="/nix/store/.../extra-container"),
                _fail(stderr="Failed to connect"),  # machinectl broken
                _ok(stdout="container@.service static"),
                _ok(stdout="zfs-2.2.0"),
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: machinectl" in result.output

    async def test_container_service_template_missing(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="/nix/store/.../extra-container"),
                _ok(),
                _ok(stdout="0 unit files listed."),  # no template
                _ok(stdout="zfs-2.2.0"),
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: container@.service template not found" in result.output
        assert "boot.enableContainers" in result.output

    async def test_zfs_unavailable(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="/nix/store/.../extra-container"),
                _ok(),
                _ok(stdout="container@.service static"),
                _fail(stderr="command not found"),  # zfs missing
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: zfs command failed" in result.output

    async def test_timeout_on_extra_container_check(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),  # which extra-container
                _ok(),
                _ok(stdout="container@.service static"),
                _ok(stdout="zfs-2.2.0"),
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: extra-container check timed out" in result.output

    async def test_timeout_on_machinectl(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="/nix/store/.../extra-container"),
                TimeoutError("timed out"),  # machinectl
                _ok(stdout="container@.service static"),
                _ok(stdout="zfs-2.2.0"),
            ]
            result = await check_host_health()

        assert result.success is False
        assert "FAIL: machinectl timed out" in result.output

    async def test_multiple_failures(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # extra-container
                _fail(),  # machinectl
                _ok(stdout="container@.service static"),
                _fail(),  # zfs
            ]
            result = await check_host_health()

        assert result.success is False
        assert result.output.count("FAIL:") >= 3


# ── get_container_logs ────────────────────────────────────────────────────────


class TestGetContainerLogs:
    """Log retrieval from container journals with fallback to host journal."""

    async def test_machine_journal_success(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="Jun 10 12:00:00 dev systemd: Started.")
            result = await get_container_logs("dev")

        assert result.success is True
        assert "Started" in result.output
        # Should use -M flag for machine journal
        call_args = mock_cmd.call_args_list[0]
        assert "-Mdev" in call_args[0]

    async def test_falls_back_to_host_journal(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machine journal fails
                _ok(stdout="Jun 10 12:00:00 host container@dev: started"),  # host journal
            ]
            result = await get_container_logs("dev")

        assert result.success is True
        assert "host journal" in result.output
        assert "started" in result.output

    async def test_machine_journal_timeout_falls_back(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),  # machine journal
                _ok(stdout="some log output"),  # host journal
            ]
            result = await get_container_logs("dev")

        assert result.success is True
        assert "some log output" in result.output

    async def test_both_journals_fail(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(stderr="No such machine"),
                _fail(stderr="No data available"),
            ]
            result = await get_container_logs("dev")

        assert result.success is False
        assert result.error is not None
        assert "dev" in result.error

    async def test_both_journals_timeout(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),
                TimeoutError("timed out"),
            ]
            result = await get_container_logs("dev")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    async def test_custom_line_count(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="log line")
            await get_container_logs("dev", lines=10)

        call_args = mock_cmd.call_args_list[0]
        assert "-n10" in call_args[0]

    async def test_line_count_capped_at_200(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="log line")
            await get_container_logs("dev", lines=999)

        call_args = mock_cmd.call_args_list[0]
        assert "-n200" in call_args[0]

    async def test_default_line_count(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="log line")
            await get_container_logs("dev")

        call_args = mock_cmd.call_args_list[0]
        assert f"-n{DEFAULT_LOG_LINES}" in call_args[0]

    async def test_empty_host_journal_returns_no_entries(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machine journal fails
                _ok(stdout=""),  # empty host journal
            ]
            result = await get_container_logs("dev")

        assert result.success is True
        assert "No log entries" in result.output

    async def test_empty_machine_journal_falls_back(self):
        """Machine journal returns success but empty stdout — should fall back."""
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout=""),  # machine journal empty
                _ok(stdout="host log line"),  # host journal
            ]
            result = await get_container_logs("dev")

        assert result.success is True
        assert "host log line" in result.output


# ── get_container_status ──────────────────────────────────────────────────────


class TestGetContainerStatus:
    """Container status via machinectl with systemctl fallback."""

    async def test_running_container_via_machinectl(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="Machine: dev\nState: running\nLeader: 1234")
            result = await get_container_status("dev")

        assert result.success is True
        assert "running" in result.output
        # machinectl status <name> should be the first call
        call_args = mock_cmd.call_args_list[0]
        assert "machinectl" in call_args[0]
        assert "dev" in call_args[0]

    async def test_stopped_container_via_systemctl(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machinectl status — container not running
                CommandResult(
                    stdout=(
                        "container@dev.service - NixOS Container\n"
                        "  Loaded: loaded\n  Active: inactive (dead)"
                    ),
                    stderr="",
                    returncode=3,  # systemctl returns 3 for inactive
                ),
            ]
            result = await get_container_status("dev")

        assert result.success is True
        assert "inactive" in result.output

    async def test_container_not_found(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machinectl
                CommandResult(stdout="", stderr="", returncode=4),  # systemctl — not found
            ]
            result = await get_container_status("ghost")

        assert result.success is False
        assert result.error is not None
        assert "ghost" in result.error

    async def test_machinectl_timeout_falls_back(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),
                _ok(stdout="Active: active (running)"),
            ]
            result = await get_container_status("dev")

        assert result.success is True
        assert "active" in result.output

    async def test_both_timeout(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),
                TimeoutError("timed out"),
            ]
            result = await get_container_status("dev")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error


# ── get_tailscale_status ──────────────────────────────────────────────────────


class TestGetTailscaleStatus:
    """Tailscale status inside a container or on the host."""

    async def test_container_tailscale_status(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="100.83.13.65 dev dev.tail1234.ts.net linux -")
            result = await get_tailscale_status("dev")

        assert result.success is True
        assert "100.83.13.65" in result.output
        # Should use nixos-container run
        call_args = mock_cmd.call_args_list[0]
        assert "nixos-container" in call_args[0]
        assert "dev" in call_args[0]
        assert "tailscale" in call_args[0]

    async def test_host_tailscale_status(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="100.10.0.1 host")
            result = await get_tailscale_status(None)

        assert result.success is True
        assert "100.10.0.1" in result.output
        # Should NOT use nixos-container run
        call_args = mock_cmd.call_args_list[0]
        assert "nixos-container" not in call_args[0]
        assert "tailscale" in call_args[0]

    async def test_tailscale_not_running(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _fail(stderr="tailscale is not running")
            result = await get_tailscale_status("dev")

        assert result.success is False
        assert "not running" in result.output

    async def test_tailscale_timeout(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = TimeoutError("timed out")
            result = await get_tailscale_status("dev")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    async def test_no_output_returns_placeholder(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="")
            result = await get_tailscale_status("dev")

        assert result.success is True
        assert result.output == "(no output)"


# ── get_service_status ────────────────────────────────────────────────────────


class TestGetServiceStatus:
    """Host service status checks with allowlist enforcement."""

    async def test_allowed_service_running(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(
                stdout="voxnix-agent.service - Voxnix Agent\n  Active: active (running)"
            )
            result = await get_service_status("voxnix-agent")

        assert result.success is True
        assert "active (running)" in result.output

    async def test_allowed_service_inactive(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = CommandResult(
                stdout="tailscaled.service\n  Active: inactive (dead)",
                stderr="",
                returncode=3,
            )
            result = await get_service_status("tailscaled")

        # Exit code 3 = inactive; still a valid diagnostic result
        assert result.success is True
        assert "inactive" in result.output

    async def test_disallowed_service_rejected(self):
        result = await get_service_status("mysql")

        assert result.success is False
        assert result.error is not None
        assert "not in the allowed" in result.error
        assert "mysql" in result.error

    async def test_all_allowed_services_accepted(self):
        """Every service in the allowlist should be accepted."""
        allowed = {
            "voxnix-agent",
            "tailscaled",
            "nix-daemon",
            "systemd-machined",
            "systemd-networkd",
            "sshd",
        }
        for svc in allowed:
            with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
                mock_cmd.return_value = _ok(stdout=f"{svc}.service - Active: active")
                result = await get_service_status(svc)
                assert result.success is True, f"Service '{svc}' should be allowed"

    async def test_service_timeout(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = TimeoutError("timed out")
            result = await get_service_status("nix-daemon")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    async def test_no_output_returns_error(self):
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = CommandResult(stdout="", stderr="", returncode=4)
            result = await get_service_status("sshd")

        assert result.success is False
        assert result.error is not None
        assert "sshd" in result.error

    async def test_uses_service_suffix(self):
        """The command should query <name>.service, not just <name>."""
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="active")
            await get_service_status("nix-daemon")

        call_args = mock_cmd.call_args_list[0]
        assert "nix-daemon.service" in call_args[0]

    async def test_stderr_used_when_stdout_empty(self):
        """systemctl sometimes puts info on stderr for non-zero exit codes."""
        with patch("agent.tools.diagnostics.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = CommandResult(
                stdout="",
                stderr="Unit sshd.service could not be found.",
                returncode=4,
            )
            result = await get_service_status("sshd")

        # stderr has content, so it should be used
        assert result.success is True
        assert "could not be found" in result.output
