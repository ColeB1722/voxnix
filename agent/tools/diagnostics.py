"""Diagnostic tools — give the agent the ability to self-diagnose failures.

Instead of surfacing vague error messages to the user, the agent can now
inspect its own environment: check host health, read container logs, query
Tailscale status, and inspect systemd service state.

All tools are read-only CLI wrappers that go through run_command(). They
expose structured results that the agent can reason about and translate
into plain-language explanations for the user.

Design decisions:
  - Fixed set of safe, read-only queries — NOT a general shell.
    The agent cannot execute arbitrary commands.
  - Output is truncated to keep LLM context windows manageable.
    Container logs default to the last 50 lines; callers can adjust.
  - All commands use short timeouts — diagnostics should be fast.
    If a diagnostic itself times out, that's useful information.
  - Logfire spans wrap every diagnostic call for observability.

See #47 (diagnostic tools for the agent) and docs/architecture.md §
Agent Behavior — Error Handling & Self-Correction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import logfire

from agent.tools.cli import run_command

logger = logging.getLogger(__name__)

# Maximum lines returned from log queries to avoid blowing up
# the LLM context window. 50 lines is enough for most diagnostics.
DEFAULT_LOG_LINES: int = 50

# Short timeout for diagnostic commands — they should be instant.
_DIAG_TIMEOUT: float = 15.0


@dataclass
class DiagnosticResult:
    """Structured result from a diagnostic query.

    The agent uses this to reason about the system state and compose
    a response for the user.
    """

    success: bool
    output: str
    error: str | None = field(default=None)


# ── Host health ───────────────────────────────────────────────────────────────


async def check_host_health() -> DiagnosticResult:
    """Run a checklist of host-level health indicators.

    Checks (in order):
      1. Is extra-container available on PATH?
      2. Is machinectl responsive?
      3. Is the container@.service template present?
      4. Is ZFS available and responsive?

    Returns a structured result with all check outcomes. The agent can
    read this to diagnose infrastructure-level problems before they
    manifest as cryptic container creation failures.
    """
    with logfire.span("diagnostic.host_health"):
        checks: list[str] = []
        all_ok = True

        # 1. extra-container available?
        try:
            result = await run_command("which", "extra-container", timeout_seconds=_DIAG_TIMEOUT)
            if result.success:
                checks.append("OK: extra-container found at " + result.stdout.split("\n")[0])
            else:
                checks.append("FAIL: extra-container not found on PATH")
                all_ok = False
        except TimeoutError:
            checks.append("FAIL: extra-container check timed out")
            all_ok = False

        # 2. machinectl responsive?
        try:
            result = await run_command(
                "machinectl", "list", "--no-pager", timeout_seconds=_DIAG_TIMEOUT
            )
            if result.success:
                checks.append("OK: machinectl is responsive")
            else:
                checks.append(f"FAIL: machinectl returned exit {result.returncode}")
                all_ok = False
        except TimeoutError:
            checks.append("FAIL: machinectl timed out — systemd-machined may be stuck")
            all_ok = False

        # 3. container@.service template present?
        try:
            result = await run_command(
                "systemctl",
                "list-unit-files",
                "container@.service",
                "--no-pager",
                timeout_seconds=_DIAG_TIMEOUT,
            )
            if result.success and "container@.service" in result.stdout:
                checks.append("OK: container@.service template found")
            else:
                checks.append(
                    "FAIL: container@.service template not found — "
                    "is boot.enableContainers = true set?"
                )
                all_ok = False
        except TimeoutError:
            checks.append("FAIL: systemctl check timed out")
            all_ok = False

        # 4. ZFS available?
        try:
            result = await run_command("zfs", "version", timeout_seconds=_DIAG_TIMEOUT)
            if result.success:
                version_line = result.stdout.split("\n")[0] if result.stdout else "unknown"
                checks.append(f"OK: ZFS available ({version_line})")
            else:
                checks.append("FAIL: zfs command failed — ZFS may not be installed or loaded")
                all_ok = False
        except TimeoutError:
            checks.append("FAIL: zfs version check timed out")
            all_ok = False

        output = "\n".join(checks)
        summary = "All checks passed." if all_ok else "Some checks failed — see details above."

        logfire.info(
            "Host health check: {status}",
            status="healthy" if all_ok else "unhealthy",
            checks=output,
        )

        return DiagnosticResult(
            success=all_ok,
            output=f"{output}\n\n{summary}",
        )


# ── Container logs ────────────────────────────────────────────────────────────


async def get_container_logs(
    name: str,
    lines: int = DEFAULT_LOG_LINES,
) -> DiagnosticResult:
    """Retrieve recent journal logs for a container.

    Uses `journalctl -M <name>` to read the container's journal directly.
    Falls back to `journalctl -u container@<name>` if the machine journal
    is not available (container stopped or journal not persistent).

    Args:
        name: Container name.
        lines: Number of recent log lines to return. Capped at 200 to
               prevent context window overflow.

    Returns:
        DiagnosticResult with log output or error description.
    """
    # Cap lines to prevent absurdly large responses.
    lines = min(lines, 200)

    with logfire.span("diagnostic.container_logs", container_name=name, lines=lines):
        # Try the container's own journal first (most detailed).
        try:
            result = await run_command(
                "journalctl",
                f"-M{name}",
                f"-n{lines}",
                "--no-pager",
                "-o",
                "short-iso",
                timeout_seconds=_DIAG_TIMEOUT,
            )
            if result.success and result.stdout:
                return DiagnosticResult(success=True, output=result.stdout)
        except TimeoutError:
            pass

        # Fall back to the host-side service unit journal.
        try:
            result = await run_command(
                "journalctl",
                "-u",
                f"container@{name}.service",
                f"-n{lines}",
                "--no-pager",
                "-o",
                "short-iso",
                timeout_seconds=_DIAG_TIMEOUT,
            )
            if result.success and result.stdout:
                return DiagnosticResult(
                    success=True,
                    output=f"(from host journal for container@{name}.service)\n{result.stdout}",
                )
            if result.success and not result.stdout:
                return DiagnosticResult(
                    success=True,
                    output=f"No log entries found for container '{name}'.",
                )
        except TimeoutError:
            return DiagnosticResult(
                success=False,
                output="",
                error=f"journalctl timed out querying logs for '{name}'.",
            )

        return DiagnosticResult(
            success=False,
            output="",
            error=f"Could not retrieve logs for container '{name}': {result.stderr}",
        )


# ── Container status ──────────────────────────────────────────────────────────


async def get_container_status(name: str) -> DiagnosticResult:
    """Get detailed systemd status for a container.

    Uses `machinectl status <name>` for running containers. If the
    container is not running, falls back to `systemctl status container@<name>`
    to show the unit state (inactive, failed, etc.).

    Args:
        name: Container name.

    Returns:
        DiagnosticResult with status output.
    """
    with logfire.span("diagnostic.container_status", container_name=name):
        # Try machinectl status first (running containers).
        try:
            result = await run_command(
                "machinectl",
                "status",
                name,
                "--no-pager",
                timeout_seconds=_DIAG_TIMEOUT,
            )
            if result.success:
                return DiagnosticResult(success=True, output=result.stdout)
        except TimeoutError:
            pass

        # Fall back to systemctl status (stopped/failed containers).
        try:
            result = await run_command(
                "systemctl",
                "status",
                f"container@{name}.service",
                "--no-pager",
                "-l",
                timeout_seconds=_DIAG_TIMEOUT,
            )
            # systemctl status returns exit code 3 for inactive units —
            # that's not an error, it's useful information.
            output = result.stdout or result.stderr
            if output:
                return DiagnosticResult(success=True, output=output)
        except TimeoutError:
            return DiagnosticResult(
                success=False,
                output="",
                error=f"Status query timed out for container '{name}'.",
            )

        return DiagnosticResult(
            success=False,
            output="",
            error=f"Container '{name}' not found in machinectl or systemd.",
        )


# ── Tailscale status ──────────────────────────────────────────────────────────


async def get_tailscale_status(name: str | None = None) -> DiagnosticResult:
    """Query Tailscale status for a container or the host.

    If a container name is given, runs `nixos-container run <name> -- tailscale status`
    inside the container. Otherwise, runs `tailscale status` on the host.

    Args:
        name: Container name, or None for host Tailscale status.

    Returns:
        DiagnosticResult with Tailscale status output.
    """
    target = name or "host"

    with logfire.span("diagnostic.tailscale_status", target=target):
        try:
            if name:
                result = await run_command(
                    "nixos-container",
                    "run",
                    name,
                    "--",
                    "tailscale",
                    "status",
                    timeout_seconds=_DIAG_TIMEOUT,
                )
            else:
                result = await run_command(
                    "tailscale",
                    "status",
                    timeout_seconds=_DIAG_TIMEOUT,
                )
        except TimeoutError:
            return DiagnosticResult(
                success=False,
                output="",
                error=f"Tailscale status query timed out for {target}.",
            )

        if result.success:
            return DiagnosticResult(success=True, output=result.stdout or "(no output)")

        # Tailscale not running or not installed is still useful information.
        error_output = result.stderr or result.stdout or "No output"
        return DiagnosticResult(
            success=False,
            output=error_output,
            error=f"Tailscale status query failed for {target}: {error_output}",
        )


# ── Service status ────────────────────────────────────────────────────────────


async def get_service_status(service_name: str) -> DiagnosticResult:
    """Query systemd service status on the host.

    Useful for checking the agent's own service, or other host-level
    services that containers depend on (e.g. tailscaled, nix-daemon).

    Only allows querying a fixed set of safe service names to prevent
    information disclosure.

    Args:
        service_name: systemd unit name (e.g. "voxnix-agent", "tailscaled").

    Returns:
        DiagnosticResult with service status.
    """
    # Allowlist of services the agent is permitted to inspect.
    allowed_services = {
        "voxnix-agent",
        "tailscaled",
        "nix-daemon",
        "systemd-machined",
        "systemd-networkd",
        "sshd",
    }

    with logfire.span("diagnostic.service_status", service=service_name):
        if service_name not in allowed_services:
            return DiagnosticResult(
                success=False,
                output="",
                error=(
                    f"Service '{service_name}' is not in the allowed diagnostic list. "
                    f"Allowed: {', '.join(sorted(allowed_services))}."
                ),
            )

        try:
            result = await run_command(
                "systemctl",
                "status",
                f"{service_name}.service",
                "--no-pager",
                "-l",
                timeout_seconds=_DIAG_TIMEOUT,
            )
        except TimeoutError:
            return DiagnosticResult(
                success=False,
                output="",
                error=f"Status query timed out for service '{service_name}'.",
            )

        # systemctl status returns exit 3 for inactive — still useful info.
        output = result.stdout or result.stderr
        if output:
            return DiagnosticResult(success=True, output=output)

        return DiagnosticResult(
            success=False,
            output="",
            error=f"No status information available for '{service_name}'.",
        )
