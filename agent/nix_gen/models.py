"""Pydantic models for voxnix container specs.

These models define the Python side of the agent-to-Nix boundary.
A ContainerSpec serializes to JSON that nix/mkContainer.nix can consume directly.

The validation rules here mirror the constraints in mkContainer.nix:
- name: lowercase alphanumeric + hyphens, no leading/trailing hyphens
- owner: non-empty string (Telegram chat_id)
- modules: non-empty list of unique module name strings
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Valid container name: lowercase alphanumeric and hyphens, no leading/trailing hyphens.
# Must be valid for systemd-nspawn machine names.
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ContainerSpec(BaseModel):
    """Spec for creating a NixOS container via mkContainer.

    Serialized to JSON and consumed by nix/mkContainer.nix.
    The agent generates these; Nix handles the actual module composition.
    """

    name: str
    owner: str
    modules: list[str]

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
