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

from agent.config import get_settings
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


async def _apply_quota(dataset: str, quota: str) -> ZfsResult:
    """Apply a ZFS quota to a dataset.

    Wraps `zfs set quota=<quota> <dataset>`. Idempotent — setting a quota
    on a dataset that already has one just updates the value.

    Args:
        dataset: Full ZFS dataset path (e.g. "tank/users/123456789").
        quota: ZFS size string (e.g. "10G", "50G", "none" to disable).

    Returns:
        ZfsResult indicating success or failure.
    """
    with logfire.span("zfs.apply_quota", dataset=dataset, quota=quota):
        result = await run_command(
            "zfs",
            "set",
            f"quota={quota}",
            dataset,
            timeout_seconds=10,
        )

        if result.success:
            logfire.info(
                "Applied quota {quota} to dataset '{dataset}'",
                quota=quota,
                dataset=dataset,
            )
            return ZfsResult(
                success=True,
                dataset=dataset,
                message=f"Applied quota {quota} to '{dataset}'.",
            )

        logfire.error(
            "Failed to apply quota {quota} to '{dataset}'",
            quota=quota,
            dataset=dataset,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        logger.error(
            "_apply_quota failed: dataset=%s quota=%s returncode=%d stderr=%r",
            dataset,
            quota,
            result.returncode,
            result.stderr,
        )
        return ZfsResult(
            success=False,
            dataset=dataset,
            message=f"Failed to apply quota {quota} to '{dataset}'.",
            error=result.stderr or result.stdout,
        )


async def create_user_datasets(owner: str) -> ZfsResult:
    """Ensure the per-user dataset root exists with a quota applied.

    Creates tank/users/<owner> if it doesn't already exist. Idempotent —
    succeeds silently if the dataset is already present. Always applies
    the per-user quota from VoxnixSettings.zfs_user_quota (default: 10G),
    even on existing datasets, so the quota stays in sync with config changes.

    The -p flag creates all intermediate datasets (though tank/users should
    already exist from the disko layout in storage.nix).

    Args:
        owner: User identifier (Telegram chat_id).

    Returns:
        ZfsResult indicating success or failure.
    """
    dataset = _user_dataset(owner)
    quota = get_settings().zfs_user_quota

    with logfire.span("zfs.create_user_datasets", owner=owner, dataset=dataset, quota=quota):
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
            # Always set mountpoint — fixes datasets created with 'legacy' mountpoint
            # by prior runs (before this fix). Idempotent if already correct.
            mount_path = f"/tank/users/{owner}"
            await run_command("zfs", "set", f"mountpoint={mount_path}", dataset, timeout_seconds=10)
            # Always apply quota — keeps it in sync with config changes.
            quota_result = await _apply_quota(dataset, quota)
            if not quota_result.success:
                logger.error(
                    "User dataset exists but quota application failed: %s",
                    quota_result.error,
                )
            return ZfsResult(
                success=True,
                dataset=dataset,
                message=f"User dataset '{dataset}' already exists (quota: {quota}).",
            )

        # Dataset doesn't exist — create it with an explicit mountpoint so it
        # appears as a real directory on the host filesystem. Without this,
        # child datasets inherit the parent's 'legacy' mountpoint and are never
        # auto-mounted, which means the directory doesn't exist for nspawn bind mounts.
        mount_path = f"/tank/users/{owner}"
        result = await run_command(
            "zfs",
            "create",
            "-o",
            f"mountpoint={mount_path}",
            dataset,
            timeout_seconds=30,
        )

        if result.success:
            logfire.info("Created user dataset '{dataset}'", dataset=dataset)
            # Apply quota to the newly created dataset.
            quota_result = await _apply_quota(dataset, quota)
            if not quota_result.success:
                logger.error(
                    "User dataset created but quota application failed: %s",
                    quota_result.error,
                )
            return ZfsResult(
                success=True,
                dataset=dataset,
                message=f"Created user dataset '{dataset}' (quota: {quota}).",
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
            # Always set mountpoint — fixes datasets created with 'legacy' mountpoint.
            await run_command(
                "zfs", "set", f"mountpoint={mount_path}", workspace_ds, timeout_seconds=10
            )
            return ZfsResult(
                success=True,
                dataset=workspace_ds,
                message=f"Container dataset '{workspace_ds}' already exists.",
                mount_path=mount_path,
            )

        # Create the full dataset hierarchy with explicit mountpoints at each level.
        # Each dataset must have a concrete mountpoint (not 'legacy') so it appears
        # as a real directory on the host filesystem — nspawn bind mounts require the
        # host path to exist as a directory before the container starts.
        #
        # Hierarchy (all under tank/users/<owner>/):
        #   containers/                       → /tank/users/<owner>/containers
        #   containers/<name>/                → /tank/users/<owner>/containers/<name>
        #   containers/<name>/workspace       → /tank/users/<owner>/containers/<name>/workspace
        containers_ds = f"{_USERS_ROOT}/{owner}/containers"
        containers_path = f"/tank/users/{owner}/containers"
        container_ds = _container_dataset(owner, container_name)
        container_root = f"/tank/users/{owner}/containers/{container_name}"

        # Intermediate: containers/ dataset
        containers_check = await run_command(
            "zfs", "list", "-H", "-o", "name", containers_ds, timeout_seconds=10
        )
        if not containers_check.success:
            await run_command(
                "zfs",
                "create",
                "-o",
                f"mountpoint={containers_path}",
                containers_ds,
                timeout_seconds=30,
            )

        # Intermediate: containers/<name>/ dataset
        container_check = await run_command(
            "zfs", "list", "-H", "-o", "name", container_ds, timeout_seconds=10
        )
        if not container_check.success:
            await run_command(
                "zfs",
                "create",
                "-o",
                f"mountpoint={container_root}",
                container_ds,
                timeout_seconds=30,
            )

        # Leaf: containers/<name>/workspace dataset
        result = await run_command(
            "zfs",
            "create",
            "-o",
            f"mountpoint={mount_path}",
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


@dataclass
class ZfsQuotaInfo:
    """Storage usage information for a user's ZFS dataset."""

    success: bool
    owner: str
    quota: str
    used: str
    available: str
    message: str
    error: str | None = field(default=None)


async def get_user_storage_info(owner: str) -> ZfsQuotaInfo:
    """Query storage usage and quota for a user's ZFS dataset root.

    Wraps `zfs get -Hp quota,used,available tank/users/<owner>` and parses
    the machine-readable output into a structured result.

    Args:
        owner: User identifier (Telegram chat_id).

    Returns:
        ZfsQuotaInfo with quota, used, and available space — or an error
        if the dataset doesn't exist or the query fails.
    """
    dataset = _user_dataset(owner)

    with logfire.span("zfs.get_user_storage_info", owner=owner, dataset=dataset):
        result = await run_command(
            "zfs",
            "get",
            "-Hp",
            "-o",
            "property,value",
            "quota,used,available",
            dataset,
            timeout_seconds=10,
        )

        if not result.success:
            logfire.error(
                "Failed to query storage info for '{dataset}'",
                dataset=dataset,
                stderr=result.stderr,
            )
            return ZfsQuotaInfo(
                success=False,
                owner=owner,
                quota="unknown",
                used="unknown",
                available="unknown",
                message=f"Failed to query storage for user '{owner}'.",
                error=result.stderr or result.stdout,
            )

        # Parse the tab-separated output lines:
        #   quota\t<bytes|none>
        #   used\t<bytes>
        #   available\t<bytes>
        props: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                props[parts[0].strip()] = parts[1].strip()

        quota_raw = props.get("quota", "0")
        used_raw = props.get("used", "0")
        available_raw = props.get("available", "0")

        quota_str = _human_size(quota_raw)
        used_str = _human_size(used_raw)
        available_str = _human_size(available_raw)

        logfire.info(
            "Storage info for '{dataset}': quota={quota}, used={used}, available={available}",
            dataset=dataset,
            quota=quota_str,
            used=used_str,
            available=available_str,
        )
        return ZfsQuotaInfo(
            success=True,
            owner=owner,
            quota=quota_str,
            used=used_str,
            available=available_str,
            message=(
                f"Storage for user '{owner}': "
                f"used {used_str} of {quota_str} quota ({available_str} available)."
            ),
        )


def _human_size(raw: str) -> str:
    """Convert a raw ZFS byte count or 'none' to a human-readable string.

    ZFS -Hp output returns raw bytes (e.g. "10737418240") or literal strings
    like "none" or "0". This converts bytes to the nearest sensible unit.

    Args:
        raw: Raw value from `zfs get -Hp` output.

    Returns:
        Human-readable size string (e.g. "10.0G", "512M", "none").
    """
    if raw in ("none", "0", "-", ""):
        return raw if raw else "0"

    try:
        size = int(raw)
    except ValueError:
        return raw

    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024:
            if unit == "B":
                return f"{size}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024

    return f"{size:.1f}P"
