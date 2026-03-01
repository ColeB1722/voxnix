"""Container query tool — deep metadata retrieval for individual containers.

Goes beyond list_workloads (which gives name/status/address) to answer
questions like "tell me about the dev container" with rich detail:
  - Installed modules (from the Nix system closure)
  - Tailscale IP and hostname (from tailscale status inside the container)
  - Storage usage (ZFS dataset metrics)
  - Uptime and systemd state
  - Owner identity

All queries are read-only CLI wrappers through run_command(). Results are
structured for the agent to compose into natural-language responses.

Design decisions:
  - Individual async functions for each metadata facet — the agent tool
    calls query_container() which fans out in parallel for speed.
  - Graceful degradation — if one facet fails (e.g. Tailscale not installed),
    the others still return. The agent gets partial info rather than nothing.
  - Output is agent-friendly plain text, not raw CLI output. The agent
    can relay it directly or summarise further.

See #54 (container query) and docs/architecture.md § Agent Tool Architecture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import logfire

from agent.tools.cli import run_command
from agent.tools.workloads import get_container_owner
from agent.tools.zfs import _human_size, _workspace_dataset

logger = logging.getLogger(__name__)

# Short timeout for metadata queries — they should be fast.
_QUERY_TIMEOUT: float = 15.0


@dataclass
class ContainerInfo:
    """Structured deep metadata for a single container.

    Fields may be None/empty when that facet is unavailable
    (e.g. no Tailscale module, container stopped, etc.).
    """

    name: str
    exists: bool
    state: str  # "running", "stopped", "not found"
    owner: str | None = None
    modules: list[str] = field(default_factory=list)
    tailscale_ip: str | None = None
    tailscale_hostname: str | None = None
    uptime: str | None = None
    storage_used: str | None = None
    storage_quota: str | None = None
    storage_available: str | None = None
    error: str | None = None

    def format_summary(self) -> str:
        """Format a plain-text summary suitable for the agent to relay to the user."""
        if not self.exists:
            return f"Container '{self.name}' does not exist."

        lines = [
            f"Container: {self.name}",
            f"State: {self.state}",
        ]

        if self.owner:
            lines.append(f"Owner: {self.owner}")

        if self.modules:
            lines.append(f"Modules: {', '.join(self.modules)}")
        else:
            lines.append("Modules: unknown (could not read from system closure)")

        if self.tailscale_ip:
            lines.append(f"Tailscale IP: {self.tailscale_ip}")
        if self.tailscale_hostname:
            lines.append(f"Tailscale hostname: {self.tailscale_hostname}")
        if not self.tailscale_ip and "tailscale" in self.modules:
            lines.append("Tailscale: module installed but status unavailable")

        if self.uptime:
            lines.append(f"Uptime: {self.uptime}")

        if self.storage_used:
            storage_parts = [f"used {self.storage_used}"]
            if self.storage_quota and self.storage_quota not in ("none", "0"):
                storage_parts.append(f"of {self.storage_quota} quota")
            if self.storage_available:
                storage_parts.append(f"({self.storage_available} available)")
            lines.append(f"Storage: {' '.join(storage_parts)}")

        if self.error:
            lines.append(f"Note: {self.error}")

        return "\n".join(lines)


# ── Individual metadata facets ────────────────────────────────────────────────


async def _query_state(name: str) -> str:
    """Determine if a container is running, stopped, or doesn't exist.

    Uses machinectl show to check running state, falls back to checking
    nixos-container list for stopped containers.
    """
    try:
        result = await run_command(
            "machinectl",
            "show",
            name,
            "--property=State",
            "--no-pager",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success and "State=" in result.stdout:
            # Output is like "State=running"
            state_val = result.stdout.split("=", 1)[1].strip()
            if state_val:
                return state_val
    except TimeoutError:
        pass

    # Check if it exists as a stopped container.
    try:
        result = await run_command(
            "nixos-container",
            "list",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success:
            names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if name in names:
                return "stopped"
    except TimeoutError:
        pass

    return "not found"


async def _query_modules(name: str) -> list[str]:
    """Read installed modules from the container's NixOS system closure.

    The mkContainer function writes VOXNIX_MODULES as a space-separated
    list into the container's environment.variables, which ends up in
    $SYSTEM_PATH/etc/set-environment as:
        export VOXNIX_MODULES="git fish tailscale workspace"

    For running containers, we query this via nixos-container run.
    For stopped containers, we read it from the Nix store directly.
    """

    # Try running container first.
    try:
        result = await run_command(
            "nixos-container",
            "run",
            name,
            "--",
            "sh",
            "-c",
            "echo $VOXNIX_MODULES",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success and result.stdout.strip():
            return result.stdout.strip().split()
    except TimeoutError:
        pass

    # Fall back to reading from Nix store (works for stopped containers).
    conf_path = Path(f"/etc/nixos-containers/{name}.conf")
    try:
        conf_text = conf_path.read_text()
        m = re.search(r"^SYSTEM_PATH=(.+)$", conf_text, re.MULTILINE)
        if m:
            set_env_path = Path(m.group(1).strip()) / "etc" / "set-environment"
            set_env_text = set_env_path.read_text()
            m2 = re.search(r'^export\s+VOXNIX_MODULES="([^"]*)"', set_env_text, re.MULTILINE)
            if m2:
                return m2.group(1).strip().split()
    except OSError:
        pass

    return []


async def _query_tailscale(name: str) -> tuple[str | None, str | None]:
    """Query Tailscale IP and hostname from inside a running container.

    Returns (ip, hostname) or (None, None) if unavailable.
    """
    ip_addr: str | None = None
    hostname: str | None = None

    # Get the Tailscale IP.
    try:
        result = await run_command(
            "nixos-container",
            "run",
            name,
            "--",
            "tailscale",
            "ip",
            "-4",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success and result.stdout.strip():
            ip_addr = result.stdout.strip().split("\n")[0]
    except TimeoutError:
        pass

    # Get the Tailscale hostname.
    try:
        result = await run_command(
            "nixos-container",
            "run",
            name,
            "--",
            "tailscale",
            "status",
            "--self",
            "--json",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                # Self node info is at the top level in tailscale status --self --json
                hostname = data.get("Self", {}).get("DNSName", "").rstrip(".")
                if not hostname:
                    hostname = data.get("Self", {}).get("HostName")
            except (json.JSONDecodeError, KeyError, AttributeError):
                pass
    except TimeoutError:
        pass

    return ip_addr, hostname


async def _query_uptime(name: str) -> str | None:
    """Get the uptime of a running container via its systemd unit.

    Returns a human-readable uptime string or None if unavailable.
    """
    try:
        result = await run_command(
            "systemctl",
            "show",
            f"container@{name}.service",
            "--property=ActiveEnterTimestamp",
            "--no-pager",
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if result.success and "ActiveEnterTimestamp=" in result.stdout:
            timestamp = result.stdout.split("=", 1)[1].strip()
            if timestamp:
                return f"since {timestamp}"
    except TimeoutError:
        pass

    return None


async def _query_storage(owner: str, name: str) -> tuple[str | None, str | None, str | None]:
    """Query ZFS storage usage for a container's workspace dataset.

    Returns (used, quota, available) as human-readable strings, or
    (None, None, None) if the dataset doesn't exist or the query fails.
    """
    dataset = _workspace_dataset(owner, name)

    try:
        result = await run_command(
            "zfs",
            "get",
            "-Hp",
            "-o",
            "property,value",
            "used,quota,available",
            dataset,
            timeout_seconds=_QUERY_TIMEOUT,
        )
        if not result.success:
            return None, None, None

        props: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                props[parts[0].strip()] = parts[1].strip()

        used = _human_size(props.get("used", "0"))
        quota = _human_size(props.get("quota", "0"))
        available = _human_size(props.get("available", "0"))
        return used, quota, available
    except TimeoutError:
        return None, None, None


# ── Main query function ──────────────────────────────────────────────────────


async def query_container(name: str, owner: str) -> ContainerInfo:
    """Query deep metadata for a single container.

    Fans out multiple metadata queries in parallel for speed, then
    assembles the results into a ContainerInfo. Individual facet
    failures are handled gracefully — the agent gets whatever
    information is available.

    Args:
        name: Container name.
        owner: Owner chat_id (for ZFS dataset path resolution and
               ownership verification).

    Returns:
        ContainerInfo with all available metadata.
    """
    with logfire.span("query.container", container_name=name, owner=owner):
        # Step 1: determine state first — it affects which facets we can query.
        state = await _query_state(name)

        if state == "not found":
            return ContainerInfo(
                name=name,
                exists=False,
                state="not found",
            )

        is_running = state == "running"
        # Step 2: fan out metadata queries in parallel.
        # Tailscale and uptime only work for running containers.
        modules_task = asyncio.create_task(_query_modules(name))
        storage_task = asyncio.create_task(_query_storage(owner, name))

        if is_running:
            tailscale_task = asyncio.create_task(_query_tailscale(name))
            uptime_task = asyncio.create_task(_query_uptime(name))
        else:
            tailscale_task = None
            uptime_task = None

        # Gather results.
        modules = await modules_task
        storage_used, storage_quota, storage_available = await storage_task

        tailscale_ip: str | None = None
        tailscale_hostname: str | None = None
        uptime: str | None = None

        if tailscale_task is not None:
            tailscale_ip, tailscale_hostname = await tailscale_task
        if uptime_task is not None:
            uptime = await uptime_task

        # Read owner from the container's system closure for verification.
        actual_owner = await get_container_owner(name)

        if actual_owner and actual_owner != owner:
            return ContainerInfo(
                name=name,
                exists=True,
                state=state,
                error="This container belongs to another user.",
            )

        info = ContainerInfo(
            name=name,
            exists=True,
            state=state,
            owner=actual_owner,
            modules=modules,
            tailscale_ip=tailscale_ip,
            tailscale_hostname=tailscale_hostname,
            uptime=uptime,
            storage_used=storage_used,
            storage_quota=storage_quota,
            storage_available=storage_available,
            error=None,
        )

        logfire.info(
            "Container query for '{container_name}': state={state}, modules={modules}",
            container_name=name,
            state=state,
            modules=modules,
        )

        return info
