"""Async subprocess runner for voxnix agent tools.

All agent tools that invoke CLI commands (nix, machinectl, nixos-container,
extra-container, zfs, etc.) go through this module. It provides:

- Structured results (stdout, stderr, returncode) via CommandResult
- Async execution via asyncio.create_subprocess_exec
- Configurable timeouts with automatic process cleanup
- Stripped output for clean parsing
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

# Default timeout for CLI commands (seconds).
# Most nix eval / machinectl commands complete in seconds;
# nix build can take much longer and should override this.
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass
class CommandResult:
    """Structured result from a CLI invocation.

    All agent tools receive one of these — never raw subprocess output.
    """

    stdout: str
    stderr: str
    returncode: int

    def __post_init__(self) -> None:
        self.stdout = self.stdout.strip()
        self.stderr = self.stderr.strip()

    @property
    def success(self) -> bool:
        """True if the command exited with code 0."""
        return self.returncode == 0


async def run_command(
    *args: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run a CLI command asynchronously and return a structured result.

    Args:
        *args: Command and arguments (e.g. "nix", "eval", ".#lib.availableModules", "--json").
        timeout_seconds: Maximum runtime before the process is killed.
            Defaults to DEFAULT_TIMEOUT_SECONDS.

    Returns:
        CommandResult with stdout, stderr, and returncode.

    Raises:
        TimeoutError: If the command exceeds timeout_seconds. The process is killed.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        cmd_str = " ".join(args)
        msg = f"Command timed out after {timeout_seconds}s: {cmd_str}"
        raise TimeoutError(msg) from None

    return CommandResult(
        stdout=stdout_bytes.decode() if stdout_bytes else "",
        stderr=stderr_bytes.decode() if stderr_bytes else "",
        returncode=proc.returncode or 0,
    )
