"""Pydantic models for voxnix container specs.

These models define the Python side of the agent-to-Nix boundary.
A ContainerSpec serializes to JSON that nix/mkContainer.nix can consume directly.

The validation rules here mirror the constraints in mkContainer.nix:
- name: lowercase alphanumeric + hyphens, no leading/trailing hyphens
- owner: non-empty string (Telegram chat_id)
- modules: non-empty list of unique module name strings

The standalone validate_container_name() function is exported for use by
agent tools that accept a container name argument outside of ContainerSpec
(e.g. destroy, start, stop). This avoids duplicating the regex and length
checks across multiple call sites.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Valid container name: lowercase alphanumeric and hyphens, no leading/trailing hyphens.
# Must be valid for systemd-nspawn machine names.
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# Maximum container name length. All voxnix containers use privateNetwork=true
# (hardcoded in nix/mkContainer.nix — it is an architectural invariant, not an option).
# The network interface name is derived from the container name (ve-<name>),
# and Linux interface names are limited to 15 characters total.
# "ve-" prefix = 3 chars, leaving 12 for the name — but NixOS enforces 11.
# See: https://github.com/NixOS/nixpkgs/issues/38509
_CONTAINER_NAME_MAX_LEN = 11


def validate_container_name(name: str) -> str | None:
    """Validate a container name outside of ContainerSpec.

    Returns an error message string if the name is invalid, or None if valid.
    Uses the same rules as ContainerSpec.validate_name — this is the shared
    entry point so validation logic is never duplicated.

    Args:
        name: Container name to validate.

    Returns:
        Error message string, or None if the name is valid.
    """
    if not name:
        return "Container name must not be empty."
    if not _CONTAINER_NAME_RE.match(name):
        return (
            f"Container name '{name}' is invalid. "
            "Must be lowercase alphanumeric with hyphens, "
            "no leading/trailing hyphens (e.g. 'my-dev')."
        )
    if len(name) > _CONTAINER_NAME_MAX_LEN:
        return (
            f"Container name '{name}' is too long ({len(name)} chars). "
            f"Must be {_CONTAINER_NAME_MAX_LEN} characters or fewer."
        )
    return None


class ContainerSpec(BaseModel):
    """Spec for creating a NixOS container via mkContainer.

    Serialized to JSON and consumed by nix/mkContainer.nix.
    The agent generates these; Nix handles the actual module composition.
    """

    name: str
    owner: str
    modules: list[str]
    workspace_path: str | None = None
    """Host-side path to bind-mount into the container at /workspace.

    Set by the agent after create_container_dataset() provisions the ZFS
    dataset. When present, mkContainer.nix adds a bindMounts entry.
    When None, the workspace module still creates /workspace as an
    ephemeral directory inside the container (no persistence).
    """

    tailscale_auth_key: str | None = None
    """Tailscale reusable auth key for container enrollment.

    Set by the agent from VoxnixSettings.tailscale_auth_key when the
    spec includes the 'tailscale' module. When present, mkContainer.nix
    injects it as environment.variables.TAILSCALE_AUTH_KEY, which the
    tailscale-autoconnect oneshot service reads on first boot.

    When None and 'tailscale' is in modules, the agent should refuse
    to create the container (missing auth key = broken Tailscale).
    """

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v:
            msg = "Container name must not be empty"
            raise ValueError(msg)
        if not _CONTAINER_NAME_RE.match(v):
            msg = (
                f"Container name '{v}' is invalid. "
                "Must be lowercase alphanumeric with hyphens, "
                "no leading/trailing hyphens (e.g. 'my-dev-container')"
            )
            raise ValueError(msg)
        if len(v) > _CONTAINER_NAME_MAX_LEN:
            msg = (
                f"Container name '{v}' is too long ({len(v)} chars). "
                f"Must be {_CONTAINER_NAME_MAX_LEN} characters or fewer — "
                "the network interface name is derived from the container name "
                "and Linux enforces a 15-character interface name limit."
            )
            raise ValueError(msg)
        return v

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, v: str) -> str:
        if not v:
            msg = "Owner must not be empty"
            raise ValueError(msg)
        return v

    @field_validator("modules")
    @classmethod
    def validate_modules(cls, v: list[str]) -> list[str]:
        if not v:
            msg = "At least one module must be specified"
            raise ValueError(msg)
        if len(v) != len(set(v)):
            dupes = [m for m in v if v.count(m) > 1]
            msg = f"Duplicate modules: {', '.join(set(dupes))}"
            raise ValueError(msg)
        return v
