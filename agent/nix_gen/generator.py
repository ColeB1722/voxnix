"""Nix expression generator — produces expressions for extra-container from ContainerSpec.

The agent never writes Nix syntax by hand. This module is the single place
where ContainerSpec (Python) is translated to a Nix expression (string) that
extra-container can evaluate and run.

Generated expression structure:
    let
      mkContainer = import /path/to/nix/mkContainer.nix;
      spec = {
        name = "dev-abc";
        owner = "chat_123";
        modules = [ "git" "fish" ];
      };
    in
      mkContainer spec

The flake path (where mkContainer.nix lives) is resolved from:
  1. The explicit `flake_path` argument (takes priority — used in tests)
  2. settings.voxnix_flake_path from VoxnixSettings (VOXNIX_FLAKE_PATH env var)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent.config import get_settings

if TYPE_CHECKING:
    from agent.nix_gen.models import ContainerSpec


def _resolve_flake_path(flake_path: str | None) -> str:
    """Resolve the flake root path from argument or settings.

    Args:
        flake_path: Explicit path override. If provided and non-empty, used directly.
            Typically only passed in tests — production code lets settings resolve it.

    Returns:
        Absolute path to the voxnix flake root.
    """
    if flake_path:
        return flake_path

    return get_settings().voxnix_flake_path


def _nix_string(value: str) -> str:
    """Wrap a Python string as a Nix string literal.

    Escapes Nix special characters within double-quoted strings:
      \\  →  \\\\   (must be first to avoid double-escaping)
      "   →  \\"
      $   →  \\$    (prevents Nix string interpolation)
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def _nix_list(items: list[str]) -> str:
    """Format a Python list of strings as a Nix list literal.

    Example: ["git", "fish"] → '[ "git" "fish" ]'
    """
    return "[ " + " ".join(_nix_string(item) for item in items) + " ]"


def generate_container_expr(
    spec: ContainerSpec,
    flake_path: str | None = None,
) -> str:
    """Generate a Nix expression for extra-container from a ContainerSpec.

    The returned expression can be written to a temporary .nix file and
    passed to `extra-container create --start /tmp/voxnix-<uuid>.nix`.

    Args:
        spec: Validated container specification produced by the agent.
        flake_path: Path to the voxnix flake root (e.g. /var/lib/voxnix).
            If omitted, resolved from VOXNIX_FLAKE_PATH via VoxnixSettings.

    Returns:
        A Nix expression string that evaluates to a container configuration.
    """
    resolved_path = _resolve_flake_path(flake_path)
    mk_container_path = str(Path(resolved_path) / "nix" / "mkContainer.nix")

    modules_nix = _nix_list(spec.modules)

    # Build optional spec fields — only included when set.
    optional_fields = ""
    if spec.workspace_path:
        optional_fields += f"\n    workspace = {_nix_string(spec.workspace_path)};"
    if spec.tailscale_auth_key:
        optional_fields += f"\n    tailscaleAuthKey = {_nix_string(spec.tailscale_auth_key)};"

    return f"""\
let
  mkContainer = import {mk_container_path};
  spec = {{
    name = {_nix_string(spec.name)};
    owner = {_nix_string(spec.owner)};
    modules = {modules_nix};{optional_fields}
  }};
in
  mkContainer spec
"""
