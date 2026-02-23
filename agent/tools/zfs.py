"""ZFS dataset management tools — wraps zfs CLI for per-user persistent storage.

These tools manage the ZFS dataset hierarchy that provides persistent workspaces
for containers. The layout follows the architecture doc (§ Host Storage — ZFS):

    tank/users/<owner>/                                    # per-user root
    tank/users/<owner>/containers/<name>/workspace         # bind-mounted into container

The agent calls these tools during the container lifecycle:
  - create_container_dataset() before container creation (so the bind mount target exists)
  - destroy_container_dataset() after container destruction (cleanup)

All CLI invocations go through run_command() from agent.tools.cli.
Observability: every ZFS operation is wrapped in a logfire.span().

See docs/architecture.md § Persistence Model and § Host Storage — ZFS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import logfire

from agent.tools.cli import run_command

logger = logging.getLogger(__name__)

# ZFS pool and dataset prefix — matches the disko layout in nix/host/storage.nix.
_POOL = "tank"
_USERS_ROOT = f"{_POOL}/users"


@dataclass
class ZfsResult:
    """Structured result from a ZFS dataset operation.

    Returned by all ZFS tools — the agent uses this to decide whether
    to proceed with container creation or report an error.
    """

    success: bool
    dataset: str
    message: str
    mount_path: str | None = field(default=None)
    error: str | None = field(default=None)


def _user_dataset(owner: str) -> str:
    """Return the ZFS dataset path for a user's root dataset."""
    return f"{_USERS_ROOT}/{owner}"


def _container_dataset(owner: str, container_name: str) -> str:
    """Return the ZFS dataset path for a container's root dataset."""
    return f"{_USERS_ROOT}/{owner}/containers/{container_name}"


def _workspace_dataset(owner: str, container_name: str) -> str:
    """Return the ZFS dataset path for a container's workspace dataset."""
    return f"{_USERS_ROOT}/{owner}/containers/{container_name}/workspace"


def _workspace_mount_path(owner: str, container_name: str) -> str:
    """Return the host-side mount path for a container's workspace.

    ZFS datasets under tank/users are mounted at /tank/users/... (see storage.nix).
    The workspace dataset's mountpoint follows the same convention.
    """
    return f"/tank/users/{owner}/containers/{container_name}/workspace"


async def create_user_datasets(owner: str) -> ZfsResult:
    """Ensure the per-user dataset root exists.

    Creates tank/users/<owner> if it doesn't already exist. Idempotent —
    succeeds silently if the dataset is already present.

    The -p flag creates all intermediate datasets (though tank/users should
    already exist from the disko layout in storage.nix).

    Args:
        owner: User identifier (Telegram chat_id).

    Returns:
        ZfsResult indicating success or failure.
    """
    dataset = _user_dataset(owner)

    with logfire.span("zfs.create_user_datasets", owner=owner, dataset=dataset):
        # Check if dataset already exists — zfs list returns 0 if it does.
        check = await run_command(
            "zfs",
            "list",
            "-H",
            "-o",
            "name",
            dataset,
            timeout_seconds=10,
        )
        if check.success:
            logfire.info(
                "User dataset '{dataset}' already exists",
                dataset=dataset,
            )
            return ZfsResult(
                success=True,
                dataset=dataset,
                message=f"User dataset '{dataset}' already exists.",
            )

        # Dataset doesn't exist — create it.
        result = await run_command(
            "zfs",
            "create",
            "-p",
            dataset,
            timeout_seconds=30,
        )

        if result.success:
            logfire.info("Created user dataset '{dataset}'", dataset=dataset)
            return ZfsResult(
                success=True,
                dataset=dataset,
                message=f"Created user dataset '{dataset}'.",
            )

        logfire.error(
            "Failed to create user dataset '{dataset}'",
            dataset=dataset,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        logger.error(
            "create_user_datasets failed: dataset=%s returncode=%d stderr=%r",
            dataset,
            result.returncode,
            result.stderr,
        )
        return ZfsResult(
            success=False,
            dataset=dataset,
            message=f"Failed to create user dataset '{dataset}'.",
            error=result.stderr or result.stdout,
        )


async def create_container_dataset(owner: str, container_name: str) -> ZfsResult:
    """Create the ZFS dataset hierarchy for a container's persistent workspace.

    Creates tank/users/<owner>/containers/<container_name>/workspace.
    Ensures the parent user dataset exists first via create_user_datasets().

    The -p flag on `zfs create` handles all intermediate datasets
    (owner, containers, container_name) in one call.

    Args:
        owner: User identifier (Telegram chat_id).
        container_name: Container name (validated by caller).

    Returns:
        ZfsResult with mount_path set on success (the host-side path to
        bind-mount into the container at /workspace).
    """
    workspace_ds = _workspace_dataset(owner, container_name)
    mount_path = _workspace_mount_path(owner, container_name)

    with logfire.span(
        "zfs.create_container_dataset",
        owner=owner,
        container_name=container_name,
        dataset=workspace_ds,
    ):
        # Ensure user root dataset exists (idempotent).
        user_result = await create_user_datasets(owner)
        if not user_result.success:
            return ZfsResult(
                success=False,
                dataset=workspace_ds,
                message=f"Failed to create container dataset: {user_result.message}",
                error=user_result.error,
            )

        # Check if workspace dataset already exists.
        check = await run_command(
            "zfs",
            "list",
            "-H",
            "-o",
            "name",
            workspace_ds,
            timeout_seconds=10,
        )
        if check.success:
            logfire.info(
                "Container dataset '{dataset}' already exists",
                dataset=workspace_ds,
            )
            return ZfsResult(
                success=True,
                dataset=workspace_ds,
                message=f"Container dataset '{workspace_ds}' already exists.",
                mount_path=mount_path,
            )

        # Create the full hierarchy with -p.
        result = await run_command(
            "zfs",
            "create",
            "-p",
            workspace_ds,
            timeout_seconds=30,
        )

        if result.success:
            logfire.info(
                "Created container dataset '{dataset}' at {mount_path}",
                dataset=workspace_ds,
                mount_path=mount_path,
            )
            return ZfsResult(
                success=True,
                dataset=workspace_ds,
                message=f"Created container dataset at '{mount_path}'.",
                mount_path=mount_path,
            )

        logfire.error(
            "Failed to create container dataset '{dataset}'",
            dataset=workspace_ds,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        logger.error(
            "create_container_dataset failed: dataset=%s returncode=%d stderr=%r",
            workspace_ds,
            result.returncode,
            result.stderr,
        )
        return ZfsResult(
            success=False,
            dataset=workspace_ds,
            message=f"Failed to create container dataset '{workspace_ds}'.",
            error=result.stderr or result.stdout,
        )


async def destroy_container_dataset(owner: str, container_name: str) -> ZfsResult:
    """Destroy a container's ZFS dataset hierarchy.

    Wraps `zfs destroy -r tank/users/<owner>/containers/<container_name>`.
    The -r flag recursively destroys the workspace and any future child
    datasets (cache, etc.).

    Does NOT destroy the user's root dataset — only the container subtree.
    If the dataset doesn't exist (already cleaned up, or never created),
    returns success.

    Args:
        owner: User identifier (Telegram chat_id).
        container_name: Container name.

    Returns:
        ZfsResult indicating success or failure.
    """
    container_ds = _container_dataset(owner, container_name)

    with logfire.span(
        "zfs.destroy_container_dataset",
        owner=owner,
        container_name=container_name,
        dataset=container_ds,
    ):
        # Check if dataset exists — if not, nothing to destroy.
        check = await run_command(
            "zfs",
            "list",
            "-H",
            "-o",
            "name",
            container_ds,
            timeout_seconds=10,
        )
        if not check.success:
            logfire.info(
                "Container dataset '{dataset}' does not exist, nothing to destroy",
                dataset=container_ds,
            )
            return ZfsResult(
                success=True,
                dataset=container_ds,
                message=f"Container dataset '{container_ds}' does not exist (already clean).",
            )

        result = await run_command(
            "zfs",
            "destroy",
            "-r",
            container_ds,
            timeout_seconds=30,
        )

        if result.success:
            logfire.info(
                "Destroyed container dataset '{dataset}'",
                dataset=container_ds,
            )
            return ZfsResult(
                success=True,
                dataset=container_ds,
                message=f"Destroyed container dataset '{container_ds}'.",
            )

        logfire.error(
            "Failed to destroy container dataset '{dataset}'",
            dataset=container_ds,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        logger.error(
            "destroy_container_dataset failed: dataset=%s returncode=%d stderr=%r",
            container_ds,
            result.returncode,
            result.stderr,
        )
        return ZfsResult(
            success=False,
            dataset=container_ds,
            message=f"Failed to destroy container dataset '{container_ds}'.",
            error=result.stderr or result.stdout,
        )
