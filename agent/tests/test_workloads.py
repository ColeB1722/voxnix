"""Tests for workload listing — machinectl output parsing and list_workloads.

TDD — these tests define the contract for how the agent queries
live workload state from systemd/machinectl.
"""

import json
from unittest.mock import patch

import pytest

from agent.tools.cli import CommandResult
from agent.tools.workloads import Workload, WorkloadError, list_workloads


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


class TestListWorkloads:
    """list_workloads calls machinectl and parses the JSON output."""

    async def test_returns_workload_list(self):
        machines = [
            {
                "machine": "dev-abc",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.2\n",
            }
        ]
        mock_result = CommandResult(stdout=json.dumps(machines), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result):
            workloads = await list_workloads()

        assert len(workloads) == 1
        assert workloads[0].name == "dev-abc"
        assert workloads[0].class_ == "container"
        assert workloads[0].state == "running"

    async def test_returns_empty_list_when_no_machines(self):
        mock_result = CommandResult(stdout=json.dumps([]), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result):
            workloads = await list_workloads()

        assert workloads == []

    async def test_parses_multiple_workloads(self):
        machines = [
            {
                "machine": "dev-abc",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.2\n",
            },
            {
                "machine": "dev-xyz",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.3\n",
            },
        ]
        mock_result = CommandResult(stdout=json.dumps(machines), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result):
            workloads = await list_workloads()

        assert len(workloads) == 2
        names = {w.name for w in workloads}
        assert names == {"dev-abc", "dev-xyz"}

    async def test_parses_addresses(self):
        """Addresses come as newline-separated strings from machinectl."""
        machines = [
            {
                "machine": "dev-abc",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.2\nfe80::1\n",
            }
        ]
        mock_result = CommandResult(stdout=json.dumps(machines), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result):
            workloads = await list_workloads()

        assert "10.100.0.2" in workloads[0].addresses
        assert "fe80::1" in workloads[0].addresses

    async def test_missing_addresses_field(self):
        """Some entries may not have an addresses field."""
        machines = [
            {
                "machine": "dev-abc",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
            }
        ]
        mock_result = CommandResult(stdout=json.dumps(machines), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result):
            workloads = await list_workloads()

        assert workloads[0].addresses == []

    async def test_machinectl_failure_raises(self):
        mock_result = CommandResult(
            stdout="",
            stderr="Failed to list machines: Permission denied",
            returncode=1,
        )

        with (
            patch("agent.tools.workloads.run_command", return_value=mock_result),
            pytest.raises(WorkloadError, match="machinectl"),
        ):
            await list_workloads()

    async def test_invalid_json_raises(self):
        mock_result = CommandResult(stdout="not json", stderr="", returncode=0)

        with (
            patch("agent.tools.workloads.run_command", return_value=mock_result),
            pytest.raises(WorkloadError, match="parse"),
        ):
            await list_workloads()

    async def test_filters_by_owner(self):
        """list_workloads(owner=...) should return only containers owned by that chat_id."""
        machines = [
            {
                "machine": "dev-abc",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.2\n",
            },
            {
                "machine": "dev-xyz",
                "class": "container",
                "service": "nspawn",
                "state": "running",
                "os": "nixos",
                "version": "25.05",
                "addresses": "10.100.0.3\n",
            },
        ]
        mock_result = CommandResult(stdout=json.dumps(machines), stderr="", returncode=0)

        with (
            patch("agent.tools.workloads.run_command", return_value=mock_result),
            patch(
                "agent.tools.workloads.get_container_owner",
                side_effect=lambda name: "chat_123" if name == "dev-abc" else "chat_456",
            ),
        ):
            workloads = await list_workloads(owner="chat_123")

        assert len(workloads) == 1
        assert workloads[0].name == "dev-abc"

    async def test_calls_correct_machinectl_command(self):
        """Verifies machinectl is called with --output=json."""
        mock_result = CommandResult(stdout=json.dumps([]), stderr="", returncode=0)

        with patch("agent.tools.workloads.run_command", return_value=mock_result) as mock_run:
            await list_workloads()

        args = mock_run.call_args[0]
        assert "machinectl" in args
        assert "--output=json" in args
