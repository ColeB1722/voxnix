"""Tests for ZFS dataset management tools.

TDD — these tests define the contract for ZFS dataset operations.
All CLI calls are mocked; no real ZFS pool is required to run these tests.
"""

import logging
from unittest.mock import AsyncMock, patch

from agent.tools.cli import CommandResult
from agent.tools.zfs import (
    ZfsResult,
    _container_dataset,
    _user_dataset,
    _workspace_dataset,
    _workspace_mount_path,
    create_container_dataset,
    create_user_datasets,
    destroy_container_dataset,
)

# ── Test constants ────────────────────────────────────────────────────────────

OWNER = "123456789"
CONTAINER = "dev-abc"
USER_DS = f"tank/users/{OWNER}"
CONTAINER_DS = f"tank/users/{OWNER}/containers/{CONTAINER}"
WORKSPACE_DS = f"tank/users/{OWNER}/containers/{CONTAINER}/workspace"
MOUNT_PATH = f"/tank/users/{OWNER}/containers/{CONTAINER}/workspace"


def ok(stdout: str = "") -> CommandResult:
    """Successful CLI result."""
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def fail(stderr: str = "error") -> CommandResult:
    """Failed CLI result."""
    return CommandResult(stdout="", stderr=stderr, returncode=1)


# ── Path helpers ──────────────────────────────────────────────────────────────


class TestPathHelpers:
    """Verify the dataset/path construction functions."""

    def test_user_dataset(self):
        assert _user_dataset(OWNER) == USER_DS

    def test_container_dataset(self):
        assert _container_dataset(OWNER, CONTAINER) == CONTAINER_DS

    def test_workspace_dataset(self):
        assert _workspace_dataset(OWNER, CONTAINER) == WORKSPACE_DS

    def test_workspace_mount_path(self):
        assert _workspace_mount_path(OWNER, CONTAINER) == MOUNT_PATH

    def test_user_dataset_different_owner(self):
        assert _user_dataset("999") == "tank/users/999"

    def test_workspace_mount_path_format(self):
        """Mount path must start with /tank/users/ — matches storage.nix layout."""
        path = _workspace_mount_path("owner1", "ctr1")
        assert path.startswith("/tank/users/")
        assert path.endswith("/workspace")


# ── ZfsResult ─────────────────────────────────────────────────────────────────


class TestZfsResult:
    def test_success_result(self):
        result = ZfsResult(success=True, dataset="tank/test", message="Created")
        assert result.success is True
        assert result.dataset == "tank/test"
        assert result.mount_path is None
        assert result.error is None

    def test_success_with_mount_path(self):
        result = ZfsResult(
            success=True,
            dataset="tank/test",
            message="Created",
            mount_path="/tank/test",
        )
        assert result.mount_path == "/tank/test"

    def test_failure_result(self):
        result = ZfsResult(
            success=False,
            dataset="tank/test",
            message="Failed",
            error="permission denied",
        )
        assert result.success is False
        assert result.error == "permission denied"

    def test_defaults(self):
        result = ZfsResult(success=True, dataset="tank/test", message="ok")
        assert result.mount_path is None
        assert result.error is None


# ── create_user_datasets ──────────────────────────────────────────────────────


class TestCreateUserDatasets:
    async def test_creates_dataset_when_missing(self):
        mock_run = AsyncMock(side_effect=[fail("does not exist"), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert result.dataset == USER_DS

        # First call: zfs list (check existence) — should fail
        # Second call: zfs create -p — should succeed
        assert mock_run.call_count == 2
        create_call = mock_run.call_args_list[1]
        assert create_call[0] == ("zfs", "create", "-p", USER_DS)

    async def test_idempotent_when_exists(self):
        mock_run = AsyncMock(return_value=ok(USER_DS))

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert "already exists" in result.message
        # Only one call — the existence check. No create needed.
        assert mock_run.call_count == 1

    async def test_create_failure_returns_error(self):
        mock_run = AsyncMock(side_effect=[fail("not found"), fail("permission denied")])

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is False
        assert result.error is not None
        assert "permission denied" in result.error

    async def test_create_failure_logs_to_logger(self, caplog):
        mock_run = AsyncMock(side_effect=[fail("not found"), fail("no space")])

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_user_datasets(OWNER)

        assert any("create_user_datasets failed" in r.message for r in caplog.records)

    async def test_uses_dash_p_flag(self):
        """The -p flag creates intermediate datasets automatically."""
        mock_run = AsyncMock(side_effect=[fail("not found"), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_user_datasets(OWNER)

        create_call = mock_run.call_args_list[1]
        assert "-p" in create_call[0]


# ── create_container_dataset ──────────────────────────────────────────────────


class TestCreateContainerDataset:
    async def test_creates_workspace_dataset(self):
        """Full success path: user exists, workspace doesn't, create succeeds."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # create_user_datasets: zfs list → exists
                fail("nope"),  # create_container_dataset: zfs list workspace → doesn't exist
                ok(),  # create_container_dataset: zfs create -p → success
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH
        assert result.dataset == WORKSPACE_DS

    async def test_idempotent_when_workspace_exists(self):
        """Workspace dataset already exists — no create needed."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # create_user_datasets: exists
                ok(WORKSPACE_DS),  # workspace check: exists
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH
        assert "already exists" in result.message
        assert mock_run.call_count == 2  # No create call

    async def test_user_dataset_creation_failure_propagates(self):
        """If user dataset creation fails, container dataset creation aborts."""
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),  # create_user_datasets: zfs list → not found
                fail("permission denied"),  # create_user_datasets: zfs create → fails
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.mount_path is None

    async def test_workspace_create_failure(self):
        """User exists, but workspace dataset creation fails."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # user exists
                fail("nope"),  # workspace doesn't exist
                fail("quota"),  # workspace create fails
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.error is not None

    async def test_workspace_create_failure_logs_to_logger(self, caplog):
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),
                fail("nope"),
                fail("out of space"),
            ]
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_container_dataset(OWNER, CONTAINER)

        assert any("create_container_dataset failed" in r.message for r in caplog.records)

    async def test_creates_full_hierarchy_with_dash_p(self):
        """The -p flag handles the intermediate containers/<name> datasets."""
        mock_run = AsyncMock(side_effect=[ok(USER_DS), fail("nope"), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_container_dataset(OWNER, CONTAINER)

        # The third call is zfs create -p <workspace_ds>
        create_call = mock_run.call_args_list[2]
        assert create_call[0] == ("zfs", "create", "-p", WORKSPACE_DS)

    async def test_mount_path_matches_storage_layout(self):
        """Mount path must match the disko layout in storage.nix."""
        mock_run = AsyncMock(side_effect=[ok(USER_DS), fail("nope"), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.mount_path is not None
        assert result.mount_path.startswith("/tank/users/")
        assert OWNER in result.mount_path
        assert CONTAINER in result.mount_path
        assert result.mount_path.endswith("/workspace")


# ── destroy_container_dataset ─────────────────────────────────────────────────


class TestDestroyContainerDataset:
    async def test_destroys_existing_dataset(self):
        mock_run = AsyncMock(
            side_effect=[
                ok(CONTAINER_DS),  # zfs list → exists
                ok(),  # zfs destroy -r → success
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.dataset == CONTAINER_DS

    async def test_calls_zfs_destroy_recursive(self):
        mock_run = AsyncMock(side_effect=[ok(CONTAINER_DS), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            await destroy_container_dataset(OWNER, CONTAINER)

        destroy_call = mock_run.call_args_list[1]
        assert destroy_call[0] == ("zfs", "destroy", "-r", CONTAINER_DS)

    async def test_succeeds_when_dataset_does_not_exist(self):
        """No dataset to destroy — treat as success (already clean)."""
        mock_run = AsyncMock(return_value=fail("does not exist"))

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert "does not exist" in result.message
        # Only one call — the existence check. No destroy needed.
        assert mock_run.call_count == 1

    async def test_destroy_failure_returns_error(self):
        mock_run = AsyncMock(
            side_effect=[
                ok(CONTAINER_DS),  # exists
                fail("busy"),  # destroy fails
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.error is not None
        assert "busy" in result.error

    async def test_destroy_failure_logs_to_logger(self, caplog):
        mock_run = AsyncMock(side_effect=[ok(CONTAINER_DS), fail("dataset is busy")])

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await destroy_container_dataset(OWNER, CONTAINER)

        assert any("destroy_container_dataset failed" in r.message for r in caplog.records)

    async def test_destroys_container_root_not_user_root(self):
        """Only the container subtree is destroyed, not the user root."""
        mock_run = AsyncMock(side_effect=[ok(CONTAINER_DS), ok()])

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        # The destroyed dataset should be the container root, NOT the user root.
        assert result.dataset == CONTAINER_DS
        assert result.dataset != USER_DS

        # The destroy command should target the container dataset.
        destroy_call = mock_run.call_args_list[1]
        assert USER_DS not in destroy_call[0][-1] or CONTAINER in destroy_call[0][-1]
