"""Tests for the container query tool — deep metadata retrieval.

TDD — these tests define the contract for query_container and its helpers:
  - _query_state: determines running/stopped/not found
  - _query_modules: reads installed modules from system closure
  - _query_tailscale: gets Tailscale IP and hostname from inside container
  - _query_uptime: reads uptime from systemd unit
  - _query_storage: queries ZFS workspace dataset metrics
  - query_container: fans out all facets in parallel, assembles ContainerInfo
  - ContainerInfo.format_summary: plain-text summary for the agent

All CLI commands and filesystem reads are mocked — no real system access required.

See #54 (container query) and docs/architecture.md § Agent Tool Architecture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from agent.tools.cli import CommandResult
from agent.tools.query import (
    ContainerInfo,
    _query_modules,
    _query_state,
    _query_storage,
    _query_tailscale,
    _query_uptime,
    query_container,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful CommandResult."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=0)


def _fail(stderr: str = "error", stdout: str = "") -> CommandResult:
    """Return a failed CommandResult."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=1)


# ── ContainerInfo.format_summary ──────────────────────────────────────────────


class TestContainerInfoFormatSummary:
    """format_summary produces a plain-text summary for the agent."""

    def test_not_found_container(self):
        info = ContainerInfo(name="ghost", exists=False, state="not found")
        assert "does not exist" in info.format_summary()
        assert "ghost" in info.format_summary()

    def test_running_container_with_full_metadata(self):
        info = ContainerInfo(
            name="dev",
            exists=True,
            state="running",
            owner="12345",
            modules=["git", "fish", "tailscale", "workspace"],
            tailscale_ip="100.83.13.65",
            tailscale_hostname="dev.tail1234.ts.net",
            uptime="since 2025-06-10 12:00:00 UTC",
            storage_used="1.5G",
            storage_quota="10.0G",
            storage_available="8.5G",
        )
        summary = info.format_summary()
        assert "Container: dev" in summary
        assert "State: running" in summary
        assert "Owner: 12345" in summary
        assert "git" in summary
        assert "fish" in summary
        assert "tailscale" in summary
        assert "Tailscale IP: 100.83.13.65" in summary
        assert "Tailscale hostname: dev.tail1234.ts.net" in summary
        assert "Uptime: since" in summary
        assert "Storage: used 1.5G" in summary
        assert "of 10.0G quota" in summary
        assert "8.5G available" in summary

    def test_stopped_container_minimal_metadata(self):
        info = ContainerInfo(
            name="stopped-ctr",
            exists=True,
            state="stopped",
            owner="99999",
            modules=["git"],
        )
        summary = info.format_summary()
        assert "State: stopped" in summary
        assert "git" in summary
        # No Tailscale, uptime, or storage
        assert "Tailscale IP" not in summary
        assert "Uptime" not in summary

    def test_no_modules_shows_unknown(self):
        info = ContainerInfo(name="dev", exists=True, state="running", modules=[])
        summary = info.format_summary()
        assert "unknown" in summary

    def test_tailscale_module_but_no_ip(self):
        info = ContainerInfo(
            name="dev",
            exists=True,
            state="running",
            modules=["tailscale"],
        )
        summary = info.format_summary()
        assert "status unavailable" in summary

    def test_storage_without_quota(self):
        info = ContainerInfo(
            name="dev",
            exists=True,
            state="running",
            storage_used="500M",
            storage_quota="none",
            storage_available="50G",
        )
        summary = info.format_summary()
        assert "used 500M" in summary
        # "none" quota should not be shown
        assert "of none" not in summary
        assert "50G available" in summary

    def test_storage_with_zero_quota(self):
        info = ContainerInfo(
            name="dev",
            exists=True,
            state="running",
            storage_used="500M",
            storage_quota="0",
            storage_available="50G",
        )
        summary = info.format_summary()
        assert "of 0 quota" not in summary

    def test_error_note_included(self):
        info = ContainerInfo(
            name="dev",
            exists=True,
            state="running",
            error="some diagnostic note",
        )
        summary = info.format_summary()
        assert "Note: some diagnostic note" in summary

    def test_no_owner_omits_owner_line(self):
        info = ContainerInfo(name="dev", exists=True, state="running")
        summary = info.format_summary()
        assert "Owner:" not in summary


# ── _query_state ──────────────────────────────────────────────────────────────


class TestQueryState:
    """Determine container state: running, stopped, or not found."""

    async def test_running_container(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="State=running")
            state = await _query_state("dev")
        assert state == "running"

    async def test_stopped_container_via_nixos_container_list(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machinectl show — not running
                _ok(stdout="dev\nother\n"),  # nixos-container list
            ]
            state = await _query_state("dev")
        assert state == "stopped"

    async def test_container_not_found(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machinectl show
                _ok(stdout="other\nanother\n"),  # nixos-container list — no "dev"
            ]
            state = await _query_state("dev")
        assert state == "not found"

    async def test_machinectl_timeout_falls_back(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),  # machinectl show
                _ok(stdout="dev\n"),  # nixos-container list
            ]
            state = await _query_state("dev")
        assert state == "stopped"

    async def test_both_timeout_returns_not_found(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),
                TimeoutError("timed out"),
            ]
            state = await _query_state("dev")
        assert state == "not found"

    async def test_machinectl_returns_non_running_state(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="State=degraded")
            state = await _query_state("dev")
        assert state == "degraded"

    async def test_empty_machinectl_output_falls_back(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="State="),  # empty state value
                _ok(stdout="dev\n"),
            ]
            state = await _query_state("dev")
        assert state == "stopped"

    async def test_nixos_container_list_empty(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),  # machinectl show
                _ok(stdout=""),  # empty list
            ]
            state = await _query_state("dev")
        assert state == "not found"


# ── _query_modules ────────────────────────────────────────────────────────────


class TestQueryModules:
    """Read installed modules from the container's environment."""

    async def test_modules_from_running_container(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="git fish tailscale workspace")
            modules = await _query_modules("dev")
        assert modules == ["git", "fish", "tailscale", "workspace"]

    async def test_single_module(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="git")
            modules = await _query_modules("dev")
        assert modules == ["git"]

    async def test_empty_output_falls_back_to_nix_store(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="")

            # Mock the filesystem fallback via pathlib.Path used inside _query_modules.
            # The function constructs Path("/etc/nixos-containers/dev.conf") and then
            # Path(system_path) / "etc" / "set-environment", so we patch at the module level.
            conf_content = "SYSTEM_PATH=/nix/store/abc-system\nAUTO_START=1\n"
            set_env_content = 'export VOXNIX_OWNER="12345"\nexport VOXNIX_MODULES="git fish"\n'

            conf_mock = MagicMock()
            conf_mock.read_text.return_value = conf_content
            set_env_mock = MagicMock()
            set_env_mock.read_text.return_value = set_env_content

            def path_factory(path_str):
                p = MagicMock()
                if path_str == "/etc/nixos-containers/dev.conf":
                    p.read_text.return_value = conf_content
                    return p
                elif path_str == "/nix/store/abc-system":
                    # Support Path(system_path) / "etc" / "set-environment"
                    etc_mock = MagicMock()
                    etc_mock.__truediv__ = MagicMock(return_value=set_env_mock)
                    p.__truediv__ = MagicMock(return_value=etc_mock)
                    return p
                return p

            with patch("agent.tools.query.Path", side_effect=path_factory):
                modules = await _query_modules("dev")

        assert modules == ["git", "fish"]

    async def test_timeout_returns_empty(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = TimeoutError("timed out")
            # Also need to handle the filesystem fallback failing
            mock_path = MagicMock()
            mock_path.return_value.read_text.side_effect = OSError("no such file")
            with patch("agent.tools.query.Path", mock_path):
                modules = await _query_modules("dev")
        assert modules == []

    async def test_command_failure_falls_back(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _fail()
            mock_path = MagicMock()
            mock_path.return_value.read_text.side_effect = OSError("no such file")
            with patch("agent.tools.query.Path", mock_path):
                modules = await _query_modules("dev")
        assert modules == []


# ── _query_tailscale ──────────────────────────────────────────────────────────


class TestQueryTailscale:
    """Query Tailscale IP and hostname from inside a container."""

    async def test_ip_and_hostname_success(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="100.83.13.65"),  # tailscale ip -4
                _ok(stdout='{"Self":{"DNSName":"dev.tail1234.ts.net.","HostName":"dev"}}'),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip == "100.83.13.65"
        assert hostname == "dev.tail1234.ts.net"

    async def test_ip_only_hostname_fails(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="100.83.13.65"),
                _fail(),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip == "100.83.13.65"
        assert hostname is None

    async def test_both_fail(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _fail(),
                _fail(),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip is None
        assert hostname is None

    async def test_ip_timeout(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                TimeoutError("timed out"),
                _ok(stdout='{"Self":{"DNSName":"dev.tail1234.ts.net."}}'),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip is None
        assert hostname == "dev.tail1234.ts.net"

    async def test_hostname_from_HostName_field(self):
        """When DNSName is empty, fall back to HostName."""
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="100.1.2.3"),
                _ok(stdout='{"Self":{"DNSName":"","HostName":"myhost"}}'),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert hostname == "myhost"

    async def test_invalid_json_returns_none_hostname(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="100.1.2.3"),
                _ok(stdout="not json at all"),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip == "100.1.2.3"
        assert hostname is None

    async def test_multiline_ip_takes_first_line(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = [
                _ok(stdout="100.83.13.65\nfd7a:115c:a1e0::1"),
                _fail(),
            ]
            ip, hostname = await _query_tailscale("dev")
        assert ip == "100.83.13.65"


# ── _query_uptime ─────────────────────────────────────────────────────────────


class TestQueryUptime:
    """Get container uptime from systemd unit timestamp."""

    async def test_uptime_success(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="ActiveEnterTimestamp=Tue 2025-06-10 12:00:00 UTC")
            uptime = await _query_uptime("dev")
        assert uptime is not None
        assert "Tue 2025-06-10" in uptime

    async def test_no_timestamp_returns_none(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _ok(stdout="ActiveEnterTimestamp=")
            uptime = await _query_uptime("dev")
        assert uptime is None

    async def test_timeout_returns_none(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.side_effect = TimeoutError("timed out")
            uptime = await _query_uptime("dev")
        assert uptime is None

    async def test_failure_returns_none(self):
        with patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd:
            mock_cmd.return_value = _fail()
            uptime = await _query_uptime("dev")
        assert uptime is None


# ── _query_storage ────────────────────────────────────────────────────────────


class TestQueryStorage:
    """Query ZFS workspace dataset metrics."""

    async def test_storage_success(self):
        with (
            patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd,
            patch(
                "agent.tools.query._workspace_dataset",
                return_value="tank/users/12345/containers/dev/workspace",
            ),
            patch("agent.tools.query._human_size", side_effect=lambda x: f"{x}B"),
        ):
            mock_cmd.return_value = _ok(
                stdout="used\t1073741824\nquota\t10737418240\navailable\t9663676416"
            )
            used, quota, available = await _query_storage("12345", "dev")
        assert used is not None
        assert quota is not None
        assert available is not None

    async def test_storage_failure_returns_nones(self):
        with (
            patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd,
            patch(
                "agent.tools.query._workspace_dataset",
                return_value="tank/users/12345/containers/dev/workspace",
            ),
        ):
            mock_cmd.return_value = _fail(stderr="dataset does not exist")
            used, quota, available = await _query_storage("12345", "dev")
        assert used is None
        assert quota is None
        assert available is None

    async def test_storage_timeout_returns_nones(self):
        with (
            patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd,
            patch(
                "agent.tools.query._workspace_dataset",
                return_value="tank/users/12345/containers/dev/workspace",
            ),
        ):
            mock_cmd.side_effect = TimeoutError("timed out")
            used, quota, available = await _query_storage("12345", "dev")
        assert used is None
        assert quota is None
        assert available is None

    async def test_storage_queries_workspace_dataset(self):
        """The query should target the workspace dataset specifically."""
        with (
            patch("agent.tools.query.run_command", new=AsyncMock()) as mock_cmd,
            patch(
                "agent.tools.query._workspace_dataset",
                return_value="tank/users/12345/containers/dev/workspace",
            ) as mock_ws,
            patch("agent.tools.query._human_size", side_effect=lambda x: x),
        ):
            mock_cmd.return_value = _ok(stdout="used\t0\nquota\t0\navailable\t0")
            await _query_storage("12345", "dev")

        # _workspace_dataset should have been called with the owner and container name
        mock_ws.assert_called_once_with("12345", "dev")
        call_args = mock_cmd.call_args_list[0]
        cmd_args = call_args[0]
        assert any("tank/users/12345/containers/dev/workspace" in arg for arg in cmd_args)


# ── query_container (integration) ─────────────────────────────────────────────


class TestQueryContainer:
    """Full container query — fans out metadata facets and assembles ContainerInfo."""

    async def test_nonexistent_container(self):
        with patch("agent.tools.query._query_state", new=AsyncMock(return_value="not found")):
            info = await query_container("ghost", owner="12345")

        assert info.exists is False
        assert info.state == "not found"
        assert info.name == "ghost"

    async def test_running_container_full_metadata(self):
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="running")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=["git", "fish", "tailscale"]),
            ),
            patch(
                "agent.tools.query._query_tailscale",
                new=AsyncMock(return_value=("100.83.13.65", "dev.ts.net")),
            ),
            patch(
                "agent.tools.query._query_uptime",
                new=AsyncMock(return_value="since 2025-06-10"),
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=("1.5G", "10G", "8.5G")),
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value="12345"),
            ),
        ):
            info = await query_container("dev", owner="12345")

        assert info.exists is True
        assert info.state == "running"
        assert info.modules == ["git", "fish", "tailscale"]
        assert info.tailscale_ip == "100.83.13.65"
        assert info.tailscale_hostname == "dev.ts.net"
        assert info.uptime == "since 2025-06-10"
        assert info.storage_used == "1.5G"
        assert info.storage_quota == "10G"
        assert info.storage_available == "8.5G"
        assert info.owner == "12345"

    async def test_stopped_container_no_tailscale_or_uptime(self):
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="stopped")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=["git"]),
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=("500M", "10G", "9.5G")),
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value="12345"),
            ),
        ):
            info = await query_container("dev", owner="12345")

        assert info.exists is True
        assert info.state == "stopped"
        assert info.tailscale_ip is None
        assert info.tailscale_hostname is None
        assert info.uptime is None
        assert info.storage_used == "500M"

    async def test_ownership_mismatch_returns_error(self):
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="running")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "agent.tools.query._query_tailscale",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch(
                "agent.tools.query._query_uptime",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=(None, None, None)),
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value="99999"),  # different owner
            ),
        ):
            info = await query_container("dev", owner="12345")

        assert info.exists is True
        assert info.error is not None
        assert "another user" in info.error

    async def test_partial_metadata_failure_still_returns_info(self):
        """If some facets fail, the others should still be present."""
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="running")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=["git"]),
            ),
            patch(
                "agent.tools.query._query_tailscale",
                new=AsyncMock(return_value=(None, None)),  # Tailscale unavailable
            ),
            patch(
                "agent.tools.query._query_uptime",
                new=AsyncMock(return_value=None),  # uptime unavailable
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=(None, None, None)),  # storage unavailable
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value="12345"),
            ),
        ):
            info = await query_container("dev", owner="12345")

        assert info.exists is True
        assert info.modules == ["git"]
        assert info.tailscale_ip is None
        assert info.storage_used is None
        # Should still have a valid summary
        summary = info.format_summary()
        assert "Container: dev" in summary
        assert "git" in summary

    async def test_owner_none_still_returns_info(self):
        """If ownership can't be determined, still return the info."""
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="running")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=["git"]),
            ),
            patch(
                "agent.tools.query._query_tailscale",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch(
                "agent.tools.query._query_uptime",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=(None, None, None)),
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value=None),  # can't determine owner
            ),
        ):
            info = await query_container("dev", owner="12345")

        assert info.exists is True
        assert info.error is None  # None owner shouldn't trigger the "another user" error

    async def test_format_summary_from_query_result(self):
        """Integration: query_container result can be formatted for the user."""
        with (
            patch("agent.tools.query._query_state", new=AsyncMock(return_value="running")),
            patch(
                "agent.tools.query._query_modules",
                new=AsyncMock(return_value=["git", "fish", "tailscale"]),
            ),
            patch(
                "agent.tools.query._query_tailscale",
                new=AsyncMock(return_value=("100.1.2.3", "dev.ts.net")),
            ),
            patch(
                "agent.tools.query._query_uptime",
                new=AsyncMock(return_value="since 2025-06-10"),
            ),
            patch(
                "agent.tools.query._query_storage",
                new=AsyncMock(return_value=("2G", "10G", "8G")),
            ),
            patch(
                "agent.tools.query.get_container_owner",
                new=AsyncMock(return_value="12345"),
            ),
        ):
            info = await query_container("dev", owner="12345")

        summary = info.format_summary()
        assert "Container: dev" in summary
        assert "running" in summary
        assert "git" in summary
        assert "100.1.2.3" in summary
        assert "dev.ts.net" in summary
        assert "2G" in summary
