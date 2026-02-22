"""Workload listing — wraps machinectl to query live container/VM state.

The agent never trusts cached workload state. Live status always comes
from systemd/machinectl, which is the ground truth.

Observability: list_workloads and get_container_owner are wrapped in
logfire.span() so ownership queries and machinectl calls appear as
discrete spans in traces, nested under the parent agent run.

See architecture.md § State Management.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import logfire

from agent.tools.cli import run_command


class WorkloadError(Exception):
    """Raised when workload listing fails."""


@dataclass
class Workload:
    """A running or stopped container/VM managed by voxnix."""

    name: str
    class_: str  # known: "container", "vm"
    service: str  # known: "nspawn", "libvirt"
    state: str  # known: "running", "stopped", "degraded", "maintenance", "failed"
    addresses: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_container(self) -> bool:
        return self.class_ == "container"

    @property
    def is_vm(self) -> bool:
        return self.class_ == "vm"


def _parse_addresses(raw: str) -> list[str]:
    """Parse the newline-separated address string from machinectl into a list."""
    return [addr.strip() for addr in raw.strip().splitlines() if addr.strip()]


def _parse_machine(entry: dict) -> Workload:
    """Parse a single machinectl JSON entry into a Workload."""
    if "machine" not in entry:
        raise WorkloadError(f"Missing 'machine' key in machinectl entry: {entry}")

    raw_addresses = entry.get("addresses", "")
    addresses = _parse_addresses(raw_addresses) if raw_addresses else []

    return Workload(
        name=entry["machine"],
        class_=entry.get("class", "container"),
        service=entry.get("service", "nspawn"),
        state=entry.get("state", "running"),
        addresses=addresses,
    )


async def get_container_owner(name: str) -> str | None:
    """Query the VOXNIX_OWNER env var inside a running container.

    Used by list_workloads(owner=...) to filter by ownership.
    Returns None if the container is not running or the env var is not set.
    """
    with logfire.span("workload.get_owner", container_name=name):
        try:
            result = await run_command(
                "nixos-container",
                "run",
                name,
                "--",
                "sh",
                "-c",
                "echo $VOXNIX_OWNER",
                timeout_seconds=10,
            )
        except TimeoutError:
            logfire.warn(
                "Owner query timed out for '{container_name}'",
                container_name=name,
            )
            return None
        if not result.success or not result.stdout:
            return None
        owner = result.stdout.strip() or None
        if owner:
            logfire.info(
                "Container '{container_name}' owned by {owner}",
                container_name=name,
                owner=owner,
            )
        return owner


async def list_workloads(*, owner: str | None = None) -> list[Workload]:
    """List all running containers and VMs managed by systemd.

    Calls `machinectl list --output=json` and parses the result into
    a list of Workload objects. Always queries live state — never cached.

    Args:
        owner: If provided, filters results to workloads owned by this
            chat_id. Ownership is determined by querying VOXNIX_OWNER
            inside each container.

    Returns:
        List of Workload objects. Empty list if no machines are running.

    Raises:
        WorkloadError: If machinectl fails or returns unparseable output.
    """
    with logfire.span("workload.list", filter_owner=owner):
        try:
            result = await run_command(
                "machinectl", "list", "--output=json", "--no-pager", timeout_seconds=15
            )
        except TimeoutError:
            raise WorkloadError(
                "machinectl timed out after 15s — is systemd-machined responsive?"
            ) from None

        if not result.success:
            raise WorkloadError(f"machinectl failed (exit {result.returncode}): {result.stderr}")

        try:
            raw = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            raise WorkloadError(f"Failed to parse machinectl output: {e}") from e

        if not isinstance(raw, list):
            raise WorkloadError(f"Expected a list from machinectl, got {type(raw).__name__}")

        workloads = [_parse_machine(entry) for entry in raw]

        if owner is None:
            logfire.info("Listed {count} workloads (unfiltered)", count=len(workloads))
            return workloads

        # Filter by ownership — query VOXNIX_OWNER inside each container in parallel.
        # Only query containers — nixos-container run does not work on VMs.
        containers = [w for w in workloads if w.is_container]
        owners = await asyncio.gather(*(get_container_owner(w.name) for w in containers))
        filtered = [w for w, o in zip(containers, owners, strict=True) if o == owner]
        logfire.info(
            "Listed {count} workloads for owner {owner} (from {total} total)",
            count=len(filtered),
            owner=owner,
            total=len(workloads),
        )
        return filtered
