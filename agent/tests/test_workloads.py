"""Tests for workload listing — machinectl output parsing and list_workloads.

TDD — these tests define the contract for how the agent queries
live workload state from systemd/machinectl and nixos-container.
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.cli import CommandResult
from agent.tools.workloads import (
    Workload,
    WorkloadError,
    _read_owner_from_system_path,
    list_workloads,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _machinectl_ok(*machines: dict) -> CommandResult:
    """CommandResult simulating a successful machinectl list call."""
    return CommandResult(stdout=json.dumps(list(machines)), stderr="", returncode=0)


def _nixos_container_list_ok(*names: str) -> CommandResult:
    """CommandResult simulating a successful nixos-container list call."""
    return CommandResult(stdout="\n".join(names), stderr="", returncode=0)


def _nixos_container_list_empty() -> CommandResult:
    return CommandResult(stdout="", stderr="", returncode=0)


_MACHINE_DEV_ABC = {
    "machine": "dev-abc",
    "class": "container",
    "service": "nspawn",
    "state": "running",
    "os": "nixos",
    "version": "25.05",
    "addresses": "10.100.0.2\n",
}

_MACHINE_DEV_XYZ = {
    "machine": "dev-xyz",
    "class": "container",
    "service": "nspawn",
    "state": "running",
    "os": "nixos",
    "version": "25.05",
    "addresses": "10.100.0.3\n",
}


# ---------------------------------------------------------------------------
# WorkloadModel
# ---------------------------------------------------------------------------


class TestWorkloadModel:
    """Workload is the structured representation of a running container or VM."""

    def test_basic_container(self):
        w = Workload(name="dev-abc", class_="container", service="nspawn", state="running")
        assert w.name == "dev-abc"
        assert w.class_ == "container"
        assert w.state == "running"

    def test_addresses_default_empty(self):
        w = Workload(name="dev-abc", class_="container", service="nspawn", state="running")
        assert w.addresses == []

    def test_addresses_parsed(self):
        w = Workload(
            name="dev-abc",
            class_="container",
            service="nspawn",
            state="running",
            addresses=["10.100.0.2"],
        )
        assert w.addresses == ["10.100.0.2"]

    def test_is_running(self):
        w = Workload(name="dev-abc", class_="container", service="nspawn", state="running")
        assert w.is_running is True

    def test_is_not_running(self):
        w = Workload(name="dev-abc", class_="container", service="nspawn", state="stopped")
        assert w.is_running is False

    def test_is_container(self):
        w = Workload(name="dev-abc", class_="container", service="nspawn", state="running")
        assert w.is_container is True
        assert w.is_vm is False

    def test_is_vm(self):
        w = Workload(name="my-vm", class_="vm", service="libvirt", state="running")
        assert w.is_vm is True
        assert w.is_container is False


# ---------------------------------------------------------------------------
# _read_owner_from_system_path
# ---------------------------------------------------------------------------


class TestReadOwnerFromSystemPath:
    """Unit tests for the host-filesystem owner resolution path."""

    def test_reads_owner_from_set_environment(self, tmp_path: Path):
        # Arrange: build a fake /etc/nixos-containers/<name>.conf + system path
        system_path = tmp_path / "nix" / "store" / "abc-nixos-system-mybox"
        etc_dir = system_path / "etc"
        etc_dir.mkdir(parents=True)
        set_env = etc_dir / "set-environment"
        set_env.write_text(
            textwrap.dedent("""\
                export EDITOR="nano"
                export VOXNIX_OWNER="chat_999"
                export VOXNIX_CONTAINER="mybox"
            """)
        )

        conf_dir = tmp_path / "etc" / "nixos-containers"
        conf_dir.mkdir(parents=True)
        conf_file = conf_dir / "mybox.conf"
        conf_file.write_text(f"SYSTEM_PATH={system_path}\nPRIVATE_NETWORK=1\n")

        with patch("agent.tools.workloads._NIXOS_CONTAINERS_CONF_DIR", conf_dir):
            owner = _read_owner_from_system_path("mybox")

        assert owner == "chat_999"

    def test_returns_none_when_conf_missing(self, tmp_path: Path):
        conf_dir = tmp_path / "etc" / "nixos-containers"
        conf_dir.mkdir(parents=True)
        # No conf file written

        with patch("agent.tools.workloads._NIXOS_CONTAINERS_CONF_DIR", conf_dir):
            owner = _read_owner_from_system_path("ghost")

        assert owner is None

    def test_returns_none_when_system_path_missing(self, tmp_path: Path):
        conf_dir = tmp_path / "etc" / "nixos-containers"
        conf_dir.mkdir(parents=True)
        # Conf points at a store path that doesn't exist
        (conf_dir / "mybox.conf").write_text(
            "SYSTEM_PATH=/nix/store/doesnotexist-nixos-system-mybox\n"
        )

        with patch("agent.tools.workloads._NIXOS_CONTAINERS_CONF_DIR", conf_dir):
            owner = _read_owner_from_system_path("mybox")

        assert owner is None

    def test_returns_none_when_no_voxnix_owner_in_set_environment(self, tmp_path: Path):
        system_path = tmp_path / "nix" / "store" / "abc-nixos-system-plain"
        etc_dir = system_path / "etc"
        etc_dir.mkdir(parents=True)
        (etc_dir / "set-environment").write_text(
            'export EDITOR="nano"\nexport LANG="en_US.UTF-8"\n'
        )

        conf_dir = tmp_path / "etc" / "nixos-containers"
        conf_dir.mkdir(parents=True)
        (conf_dir / "plain.conf").write_text(f"SYSTEM_PATH={system_path}\n")

        with patch("agent.tools.workloads._NIXOS_CONTAINERS_CONF_DIR", conf_dir):
            owner = _read_owner_from_system_path("plain")

        assert owner is None

    def test_returns_none_when_voxnix_owner_is_empty(self, tmp_path: Path):
        system_path = tmp_path / "nix" / "store" / "abc-nixos-system-empty"
        etc_dir = system_path / "etc"
        etc_dir.mkdir(parents=True)
        (etc_dir / "set-environment").write_text('export VOXNIX_OWNER=""\n')

        conf_dir = tmp_path / "etc" / "nixos-containers"
        conf_dir.mkdir(parents=True)
        (conf_dir / "empty.conf").write_text(f"SYSTEM_PATH={system_path}\n")

        with patch("agent.tools.workloads._NIXOS_CONTAINERS_CONF_DIR", conf_dir):
            owner = _read_owner_from_system_path("empty")

        assert owner is None


# ---------------------------------------------------------------------------
# ListWorkloads — running containers only
# ---------------------------------------------------------------------------


class TestListWorkloads:
    """list_workloads calls machinectl + nixos-container list and merges results."""

    async def test_returns_running_workload(self):
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(_MACHINE_DEV_ABC),
                    _nixos_container_list_empty(),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert len(workloads) == 1
        assert workloads[0].name == "dev-abc"
        assert workloads[0].state == "running"

    async def test_returns_empty_when_no_machines_and_no_containers(self):
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(),
                    _nixos_container_list_empty(),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert workloads == []

    async def test_parses_multiple_running_workloads(self):
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(_MACHINE_DEV_ABC, _MACHINE_DEV_XYZ),
                    _nixos_container_list_empty(),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert len(workloads) == 2
        assert {w.name for w in workloads} == {"dev-abc", "dev-xyz"}

    async def test_parses_addresses(self):
        machine = {**_MACHINE_DEV_ABC, "addresses": "10.100.0.2\nfe80::1\n"}
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(machine),
                    _nixos_container_list_empty(),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert "10.100.0.2" in workloads[0].addresses
        assert "fe80::1" in workloads[0].addresses

    async def test_missing_addresses_field(self):
        machine = {k: v for k, v in _MACHINE_DEV_ABC.items() if k != "addresses"}
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(machine),
                    _nixos_container_list_empty(),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert workloads[0].addresses == []

    async def test_machinectl_failure_raises(self):
        failed = CommandResult(
            stdout="", stderr="Failed to list machines: Permission denied", returncode=1
        )
        with (
            patch("agent.tools.workloads.run_command", AsyncMock(return_value=failed)),
            pytest.raises(WorkloadError, match="machinectl"),
        ):
            await list_workloads()

    async def test_invalid_json_raises(self):
        with (
            patch(
                "agent.tools.workloads.run_command",
                AsyncMock(return_value=CommandResult(stdout="not json", stderr="", returncode=0)),
            ),
            pytest.raises(WorkloadError, match="parse"),
        ):
            await list_workloads()

    async def test_calls_correct_machinectl_command(self):
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(),
                    _nixos_container_list_empty(),
                ]
            ),
        ) as mock_run:
            await list_workloads()

        first_call_args = mock_run.call_args_list[0][0]
        assert "machinectl" in first_call_args
        assert "--output=json" in first_call_args


# ---------------------------------------------------------------------------
# ListWorkloads — stopped containers
# ---------------------------------------------------------------------------


class TestListWorkloadsStopped:
    """Stopped containers from nixos-container list are included with state=stopped."""

    async def test_stopped_container_included(self):
        # machinectl sees nothing; nixos-container list sees "old-box"
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(),
                    _nixos_container_list_ok("old-box"),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert len(workloads) == 1
        assert workloads[0].name == "old-box"
        assert workloads[0].state == "stopped"
        assert workloads[0].is_container is True

    async def test_running_and_stopped_combined(self):
        # dev-abc is running; old-box is stopped
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(_MACHINE_DEV_ABC),
                    _nixos_container_list_ok("dev-abc", "old-box"),
                ]
            ),
        ):
            workloads = await list_workloads()

        assert len(workloads) == 2
        by_name = {w.name: w for w in workloads}
        assert by_name["dev-abc"].state == "running"
        assert by_name["old-box"].state == "stopped"

    async def test_running_not_duplicated(self):
        # dev-abc appears in both machinectl and nixos-container list
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(_MACHINE_DEV_ABC),
                    _nixos_container_list_ok("dev-abc"),
                ]
            ),
        ):
            workloads = await list_workloads()

        # Should appear exactly once, as running
        assert len(workloads) == 1
        assert workloads[0].state == "running"

    async def test_stopped_container_owner_filter(self):
        """Stopped containers are filtered via _read_owner_from_system_path."""
        with (
            patch(
                "agent.tools.workloads.run_command",
                AsyncMock(
                    side_effect=[
                        _machinectl_ok(),
                        _nixos_container_list_ok("my-box", "other-box"),
                    ]
                ),
            ),
            patch(
                "agent.tools.workloads._read_owner_from_system_path",
                side_effect=lambda name: "chat_123" if name == "my-box" else "chat_456",
            ),
        ):
            workloads = await list_workloads(owner="chat_123")

        assert len(workloads) == 1
        assert workloads[0].name == "my-box"
        assert workloads[0].state == "stopped"

    async def test_nixos_container_list_failure_is_non_fatal(self):
        """If nixos-container list fails, we fall back to running-only — no crash."""
        failed = CommandResult(stdout="", stderr="command not found", returncode=127)
        with patch(
            "agent.tools.workloads.run_command",
            AsyncMock(
                side_effect=[
                    _machinectl_ok(_MACHINE_DEV_ABC),
                    failed,
                ]
            ),
        ):
            workloads = await list_workloads()

        # Should still return the running container
        assert len(workloads) == 1
        assert workloads[0].name == "dev-abc"
        assert workloads[0].state == "running"


# ---------------------------------------------------------------------------
# ListWorkloads — ownership filtering (running containers)
# ---------------------------------------------------------------------------


class TestListWorkloadsOwnerFilter:
    """list_workloads(owner=...) filters by ownership using get_container_owner."""

    async def test_filters_running_by_owner(self):
        with (
            patch(
                "agent.tools.workloads.run_command",
                AsyncMock(
                    side_effect=[
                        _machinectl_ok(_MACHINE_DEV_ABC, _MACHINE_DEV_XYZ),
                        _nixos_container_list_empty(),
                    ]
                ),
            ),
            patch(
                "agent.tools.workloads.get_container_owner",
                AsyncMock(side_effect=lambda name: "chat_123" if name == "dev-abc" else "chat_456"),
            ),
        ):
            workloads = await list_workloads(owner="chat_123")

        assert len(workloads) == 1
        assert workloads[0].name == "dev-abc"

    async def test_returns_empty_when_owner_has_no_containers(self):
        with (
            patch(
                "agent.tools.workloads.run_command",
                AsyncMock(
                    side_effect=[
                        _machinectl_ok(_MACHINE_DEV_ABC),
                        _nixos_container_list_empty(),
                    ]
                ),
            ),
            patch(
                "agent.tools.workloads.get_container_owner",
                AsyncMock(return_value="chat_other"),
            ),
        ):
            workloads = await list_workloads(owner="chat_123")

        assert workloads == []

    async def test_mixed_running_and_stopped_owner_filter(self):
        """Running containers use get_container_owner; stopped use _read_owner_from_system_path."""
        with (
            patch(
                "agent.tools.workloads.run_command",
                AsyncMock(
                    side_effect=[
                        _machinectl_ok(_MACHINE_DEV_ABC),
                        _nixos_container_list_ok("dev-abc", "old-box"),
                    ]
                ),
            ),
            patch(
                "agent.tools.workloads.get_container_owner",
                AsyncMock(return_value="chat_123"),
            ),
            patch(
                "agent.tools.workloads._read_owner_from_system_path",
                side_effect=lambda name: "chat_123" if name == "old-box" else None,
            ),
        ):
            workloads = await list_workloads(owner="chat_123")

        assert len(workloads) == 2
        by_name = {w.name: w for w in workloads}
        assert by_name["dev-abc"].state == "running"
        assert by_name["old-box"].state == "stopped"
