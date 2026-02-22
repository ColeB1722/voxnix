"""Workload listing — wraps machinectl and nixos-container to query container state.

The agent never trusts cached workload state. Live status always comes
from systemd/machinectl and nixos-container, which are the ground truth.

Stopped containers are enumerated via `nixos-container list` (reads
/etc/nixos-containers/). Their ownership is resolved by reading
VOXNIX_OWNER from $SYSTEM_PATH/etc/set-environment — the NixOS system
closure baked at container creation time, readable from the host even
when the container is stopped.

Observability: list_workloads and get_container_owner are wrapped in
logfire.span() so ownership queries and machinectl calls appear as
discrete spans in traces, nested under the parent agent run.

See architecture.md § State Management.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import logfire

from agent.tools.cli import run_command

# Path where nixos-container stores per-container conf files.
# Each file is named <name>.conf and contains SYSTEM_PATH plus nspawn config.
_NIXOS_CONTAINERS_CONF_DIR = Path("/etc/nixos-containers")

# Pattern to extract VOXNIX_OWNER from $SYSTEM_PATH/etc/set-environment.
# The file contains lines like: export VOXNIX_OWNER="8586298950"
_VOXNIX_OWNER_RE = re.compile(r'^export\s+VOXNIX_OWNER="([^"]*)"', re.MULTILINE)

# Pattern to extract SYSTEM_PATH from /etc/nixos-containers/<name>.conf.
# The file contains lines like: SYSTEM_PATH=/nix/store/...
_SYSTEM_PATH_RE = re.compile(r"^SYSTEM_PATH=(.+)$", re.MULTILINE)


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


def _read_owner_from_system_path(name: str) -> str | None:
    """Read VOXNIX_OWNER from the container's NixOS system closure.

    Works for both running and stopped containers. The approach:
      1. Read /etc/nixos-containers/<name>.conf to get SYSTEM_PATH
         (the container's NixOS system closure in the Nix store)
      2. Read $SYSTEM_PATH/etc/set-environment and extract VOXNIX_OWNER

    The set-environment file is generated statically by NixOS at build time
    from environment.variables — it lives in the immutable Nix store, so it
    is always readable from the host regardless of container state.

    Returns the owner string, or None if the conf/set-environment is missing
    or does not contain VOXNIX_OWNER (e.g. a non-voxnix container).
    """
    conf_path = _NIXOS_CONTAINERS_CONF_DIR / f"{name}.conf"
    try:
        conf_text = conf_path.read_text()
    except OSError:
        return None

    m = _SYSTEM_PATH_RE.search(conf_text)
    if not m:
        return None
    system_path = Path(m.group(1).strip())

    set_env_path = system_path / "etc" / "set-environment"
    try:
        set_env_text = set_env_path.read_text()
    except OSError:
        return None

    m2 = _VOXNIX_OWNER_RE.search(set_env_text)
    if not m2:
        return None
    return m2.group(1) or None


async def _list_nixos_container_names() -> list[str]:
    """List all configured nixos-container names, running and stopped.

    Calls `nixos-container list` which reads /etc/nixos-containers/ — this
    enumerates every container that has been created, regardless of whether
    it is currently running.

    Returns an empty list (not an error) if the command fails — e.g. on a
    system where no containers have ever been created.
    """
    result = await run_command("nixos-container", "list", timeout_seconds=10)
    if not result.success or not result.stdout:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


async def get_container_owner(name: str) -> str | None:
    """Return the VOXNIX_OWNER for a container, running or stopped.

    Strategy (fastest reliable path first):
      1. Query the running container via `nixos-container run` — authoritative
         and works in <1s when the container is running.
      2. Fall back to reading VOXNIX_OWNER from the container's NixOS system
         closure on disk — works for stopped containers, zero subprocess cost.

    Args:
        name: Container name.

    Returns:
        Owner string (Telegram chat_id), or None if unavailable.
    """
    with logfire.span("workload.get_owner", container_name=name):
        # Fast path: query the running container directly.
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
            if result.success and result.stdout:
                owner = result.stdout.strip() or None
                if owner:
                    logfire.info(
                        "Container '{container_name}' owned by {owner} (live query)",
                        container_name=name,
                        owner=owner,
                    )
                    return owner
        except TimeoutError:
            logfire.warn(
                "Owner query timed out for '{container_name}', falling back to system path",
                container_name=name,
            )

        # Slow path: read from the Nix store (container stopped or query failed).
        owner = _read_owner_from_system_path(name)
        if owner:
            logfire.info(
                "Container '{container_name}' owned by {owner} (system path)",
                container_name=name,
                owner=owner,
            )
        return owner


async def list_workloads(*, owner: str | None = None) -> list[Workload]:
    """List all containers and VMs — running and stopped.

    Combines two sources:
      - `machinectl list --output=json` for running containers/VMs (full state).
      - `nixos-container list` for all configured containers, to surface stopped ones.

    Stopped containers are included with state="stopped". Ownership filtering
    for stopped containers uses the system-path fallback in get_container_owner.

    Args:
        owner: If provided, filters results to workloads owned by this chat_id.

    Returns:
        List of Workload objects (running + stopped). Empty list if none exist.

    Raises:
        WorkloadError: If machinectl fails or returns unparseable output.
    """
    with logfire.span("workload.list", filter_owner=owner):
        # ── Running containers/VMs from machinectl ─────────────────────────
        try:
            machinectl_result = await run_command(
                "machinectl", "list", "--output=json", "--no-pager", timeout_seconds=15
            )
        except TimeoutError:
            raise WorkloadError(
                "machinectl timed out after 15s — is systemd-machined responsive?"
            ) from None

        if not machinectl_result.success:
            raise WorkloadError(
                f"machinectl failed (exit {machinectl_result.returncode}): "
                f"{machinectl_result.stderr}"
            )

        try:
            raw = json.loads(machinectl_result.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            raise WorkloadError(f"Failed to parse machinectl output: {e}") from e

        if not isinstance(raw, list):
            raise WorkloadError(f"Expected a list from machinectl, got {type(raw).__name__}")

        running: dict[str, Workload] = {}
        for entry in raw:
            w = _parse_machine(entry)
            running[w.name] = w

        # ── Stopped nixos-containers (not visible in machinectl) ───────────
        all_nixos_names = await _list_nixos_container_names()
        stopped = [
            Workload(name=n, class_="container", service="nspawn", state="stopped")
            for n in all_nixos_names
            if n not in running
        ]

        workloads: list[Workload] = list(running.values()) + stopped

        if owner is None:
            logfire.info(
                "Listed {count} workloads (unfiltered, {running} running, {stopped} stopped)",
                count=len(workloads),
                running=len(running),
                stopped=len(stopped),
            )
            return workloads

        # ── Ownership filtering ────────────────────────────────────────────
        # Running containers: query in parallel (nixos-container run, fast).
        # Stopped containers: read from system path via asyncio.to_thread so
        # the blocking Path.read_text() calls don't block the event loop.
        # VMs: ownership query not supported — excluded from filtered results.
        running_workloads = [w for w in workloads if w.is_running and w.is_container]
        stopped_workloads = [w for w in workloads if not w.is_running and w.is_container]

        running_owners = await asyncio.gather(
            *(get_container_owner(w.name) for w in running_workloads)
        )
        stopped_owners = await asyncio.gather(
            *(asyncio.to_thread(_read_owner_from_system_path, w.name) for w in stopped_workloads)
        )

        filtered: list[Workload] = []
        for w, o in zip(running_workloads, running_owners, strict=True):
            if o == owner:
                filtered.append(w)
        for w, o in zip(stopped_workloads, stopped_owners, strict=True):
            if o == owner:
                filtered.append(w)

        logfire.info(
            "Listed {count} workloads for owner {owner} (from {total} total)",
            count=len(filtered),
            owner=owner,
            total=len(workloads),
        )
        return filtered
