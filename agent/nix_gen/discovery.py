"""Module discovery — queries available modules from the Nix flake.

The agent doesn't hardcode module names. Instead, it calls
`nix eval .#lib.availableModules --json` and parses the result.
This keeps Python in sync with whatever modules exist in nix/modules/.

Results are cached by default since modules don't change at runtime
(they change on deployment via nixos-rebuild).
"""

from __future__ import annotations

import json

from agent.tools.cli import CommandResult, run_command

# Module-level cache for discovered modules.
# Populated on first call to discover_modules(), cleared on process restart.
#
# Cache invalidation strategy: Option 1 (always restart agent on nixos-rebuild).
# When new modules are added to nix/modules/ and deployed via nixos-rebuild,
# the agent's systemd service is restarted, which clears this cache and ensures
# fresh module discovery on the next interaction.
#
# TODO: This means in-flight Telegram conversations are dropped on rebuild.
# Acceptable for MVP (single admin, intentional rebuilds). Revisit Option 3
# (file watcher on a generated available-modules.json) if uptime requirements
# increase with multi-user support. See docs/architecture.md § Deployment Workflow.
_cache: list[str] | None = None


class ModuleDiscoveryError(Exception):
    """Raised when module discovery fails."""


async def run_nix_eval() -> CommandResult:
    """Run `nix eval .#lib.availableModules --json`.

    Separated from discover_modules for testability — tests mock this function.

    Flags:
        --no-update-lock-file: prevents nix from trying to update flake.lock,
            which would fail in the read-only /nix/store working directory.
        timeout_seconds=120: the first eval after a cold boot needs to fetch
            flake inputs from the network; 60s is too tight.
    """
    return await run_command(
        "nix",
        "eval",
        ".#lib.availableModules",
        "--json",
        "--no-update-lock-file",
        timeout_seconds=120,
    )


async def discover_modules(*, use_cache: bool = True) -> list[str]:
    """Discover available workload modules from the Nix flake.

    Calls `nix eval .#lib.availableModules --json` and returns a sorted
    list of module name strings (e.g. ["fish", "git", "workspace"]).

    Args:
        use_cache: If True (default), returns cached result from a previous
            call if available. Set to False to force a fresh query.

    Returns:
        Sorted list of available module names.

    Raises:
        ModuleDiscoveryError: If nix eval fails, returns unparseable output,
            or returns an unexpected type.
    """
    global _cache  # noqa: PLW0603

    if use_cache and _cache is not None:
        return _cache

    result = await run_nix_eval()

    if result.returncode != 0:
        raise ModuleDiscoveryError(f"nix eval failed (exit {result.returncode}): {result.stderr}")

    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise ModuleDiscoveryError(f"Failed to parse nix eval output as JSON: {e}") from e

    if not isinstance(parsed, list):
        raise ModuleDiscoveryError(f"Expected a list of module names, got {type(parsed).__name__}")

    if not all(isinstance(m, str) for m in parsed):
        bad = [type(m).__name__ for m in parsed if not isinstance(m, str)]
        raise ModuleDiscoveryError(
            f"Expected all module names to be strings, got: {', '.join(bad)}"
        )

    modules = sorted(parsed)

    if use_cache:
        _cache = modules

    return modules


def clear_cache() -> None:
    """Clear the module discovery cache.

    Useful for testing or after a deployment that may have changed
    available modules.
    """
    global _cache  # noqa: PLW0603
    _cache = None
