"""Tests for ZFS dataset management tools.

TDD — these tests define the contract for ZFS dataset operations.
All CLI calls are mocked; no real ZFS pool is required to run these tests.

The get_settings() call in create_user_datasets (for quota) is mocked via
a module-level autouse fixture so tests don't require env vars.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.cli import CommandResult
from agent.tools.zfs import (
    ZfsQuotaInfo,
    ZfsResult,
    _container_dataset,
    _human_size,
    _user_dataset,
    _workspace_dataset,
    _workspace_mount_path,
    create_container_dataset,
    create_user_datasets,
    destroy_container_dataset,
    get_user_storage_info,
)

# ── Test constants ────────────────────────────────────────────────────────────

OWNER = "123456789"
CONTAINER = "dev-abc"
USER_DS = f"tank/users/{OWNER}"
CONTAINER_DS = f"tank/users/{OWNER}/containers/{CONTAINER}"
WORKSPACE_DS = f"tank/users/{OWNER}/containers/{CONTAINER}/workspace"
MOUNT_PATH = f"/tank/users/{OWNER}/containers/{CONTAINER}/workspace"
DEFAULT_QUOTA = "10G"


def ok(stdout: str = "") -> CommandResult:
    """Successful CLI result."""
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def fail(stderr: str = "error") -> CommandResult:
    """Failed CLI result."""
    return CommandResult(stdout="", stderr=stderr, returncode=1)


def _mock_settings(quota: str = DEFAULT_QUOTA) -> MagicMock:
    """Return a mock VoxnixSettings with the given zfs_user_quota."""
    settings = MagicMock()
    settings.zfs_user_quota = quota
    return settings


@pytest.fixture(autouse=True)
def _mock_get_settings():
    """Mock get_settings() for all tests so no env vars are required.

    Tests that need a different quota can patch again locally.
    """
    with patch("agent.tools.zfs.get_settings", return_value=_mock_settings(DEFAULT_QUOTA)):
        yield


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


# ── ZfsQuotaInfo ──────────────────────────────────────────────────────────────


class TestZfsQuotaInfo:
    def test_success_result(self):
        info = ZfsQuotaInfo(
            success=True,
            owner=OWNER,
            quota="10.0G",
            used="1.5G",
            available="8.5G",
            message="Storage info",
        )
        assert info.success is True
        assert info.quota == "10.0G"
        assert info.used == "1.5G"
        assert info.available == "8.5G"
        assert info.error is None

    def test_failure_result(self):
        info = ZfsQuotaInfo(
            success=False,
            owner=OWNER,
            quota="unknown",
            used="unknown",
            available="unknown",
            message="Failed",
            error="dataset not found",
        )
        assert info.success is False
        assert info.error == "dataset not found"


# ── _human_size ───────────────────────────────────────────────────────────────


class TestHumanSize:
    def test_none_string(self):
        assert _human_size("none") == "none"

    def test_zero(self):
        assert _human_size("0") == "0"

    def test_dash(self):
        assert _human_size("-") == "-"

    def test_empty(self):
        assert _human_size("") == "0"

    def test_bytes(self):
        assert _human_size("512") == "512B"

    def test_kilobytes(self):
        result = _human_size(str(2 * 1024))
        assert "K" in result

    def test_megabytes(self):
        result = _human_size(str(100 * 1024 * 1024))
        assert "M" in result

    def test_gigabytes(self):
        result = _human_size(str(10 * 1024 * 1024 * 1024))
        assert "G" in result
        assert result == "10.0G"

    def test_terabytes(self):
        result = _human_size(str(2 * 1024 * 1024 * 1024 * 1024))
        assert "T" in result

    def test_non_numeric_passthrough(self):
        """Non-numeric strings that aren't special values pass through unchanged."""
        assert _human_size("unknown") == "unknown"

    def test_exact_1g(self):
        assert _human_size(str(1024 * 1024 * 1024)) == "1.0G"


# ── create_user_datasets ──────────────────────────────────────────────────────


class TestCreateUserDatasets:
    async def test_creates_dataset_when_missing(self):
        mock_run = AsyncMock(
            side_effect=[
                fail("does not exist"),  # zfs list — not found
                ok(),  # zfs create -p
                ok(),  # zfs set quota=
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert result.dataset == USER_DS

        # First call: zfs list (check existence) — should fail
        # Second call: zfs create -p — should succeed
        # Third call: zfs set quota= — should succeed
        assert mock_run.call_count == 3
        create_call = mock_run.call_args_list[1]
        assert create_call[0] == ("zfs", "create", "-p", USER_DS)

    async def test_idempotent_when_exists(self):
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # zfs list — exists
                ok(),  # zfs set quota= (always applied)
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert "already exists" in result.message
        # Two calls: existence check + quota application.
        assert mock_run.call_count == 2

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
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),  # zfs list
                ok(),  # zfs create
                ok(),  # zfs set quota
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_user_datasets(OWNER)

        create_call = mock_run.call_args_list[1]
        assert "-p" in create_call[0]

    async def test_quota_applied_on_new_dataset(self):
        """Quota is applied after dataset creation."""
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),  # zfs list
                ok(),  # zfs create
                ok(),  # zfs set quota
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        quota_call = mock_run.call_args_list[2]
        assert quota_call[0] == ("zfs", "set", f"quota={DEFAULT_QUOTA}", USER_DS)

    async def test_quota_applied_on_existing_dataset(self):
        """Quota is reapplied to existing datasets (keeps config in sync)."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # zfs list — exists
                ok(),  # zfs set quota
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        quota_call = mock_run.call_args_list[1]
        assert quota_call[0] == ("zfs", "set", f"quota={DEFAULT_QUOTA}", USER_DS)

    async def test_custom_quota_from_settings(self):
        """Quota value comes from VoxnixSettings.zfs_user_quota."""
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),
                ok(),  # create
                ok(),  # quota
            ]
        )

        with (
            patch("agent.tools.zfs.get_settings", return_value=_mock_settings("50G")),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_user_datasets(OWNER)

        quota_call = mock_run.call_args_list[2]
        assert quota_call[0] == ("zfs", "set", "quota=50G", USER_DS)

    async def test_quota_failure_logged_but_success_returned(self, caplog):
        """Quota failure is logged but dataset creation still returns success."""
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),  # zfs list
                ok(),  # zfs create
                fail("invalid quota"),  # zfs set quota — fails
            ]
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            result = await create_user_datasets(OWNER)

        # Dataset was created — success. Quota failure is logged.
        assert result.success is True
        assert any("quota application failed" in r.message for r in caplog.records)

    async def test_quota_in_success_message(self):
        """Success message mentions the quota value."""
        mock_run = AsyncMock(
            side_effect=[
                fail("not found"),
                ok(),  # create
                ok(),  # quota
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert DEFAULT_QUOTA in result.message


# ── create_container_dataset ──────────────────────────────────────────────────


class TestCreateContainerDataset:
    async def test_creates_workspace_dataset(self):
        """Full success path: user exists, workspace doesn't, create succeeds."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # create_user_datasets: zfs list → exists
                ok(),  # create_user_datasets: zfs set quota
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
                ok(),  # create_user_datasets: quota
                ok(WORKSPACE_DS),  # workspace check: exists
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH
        assert "already exists" in result.message

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
                ok(),  # quota
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
                ok(),  # quota
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
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # user exists
                ok(),  # quota
                fail("nope"),  # workspace doesn't exist
                ok(),  # create
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_container_dataset(OWNER, CONTAINER)

        # The fourth call is zfs create -p <workspace_ds>
        create_call = mock_run.call_args_list[3]
        assert create_call[0] == ("zfs", "create", "-p", WORKSPACE_DS)

    async def test_mount_path_matches_storage_layout(self):
        """Mount path must match the disko layout in storage.nix."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),
                ok(),  # quota
                fail("nope"),
                ok(),
            ]
        )

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
        assert CONTAINER in destroy_call[0][-1]


# ── get_user_storage_info ─────────────────────────────────────────────────────


class TestGetUserStorageInfo:
    async def test_success(self):
        """Parses zfs get output correctly."""
        zfs_output = "quota\t10737418240\nused\t1073741824\navailable\t9663676416\n"
        mock_run = AsyncMock(return_value=ok(zfs_output))

        with patch("agent.tools.zfs.run_command", mock_run):
            info = await get_user_storage_info(OWNER)

        assert info.success is True
        assert info.owner == OWNER
        assert "G" in info.quota
        assert "G" in info.used or "M" in info.used
        assert "G" in info.available

    async def test_calls_zfs_get_with_correct_args(self):
        mock_run = AsyncMock(return_value=ok("quota\t0\nused\t0\navailable\t0\n"))

        with patch("agent.tools.zfs.run_command", mock_run):
            await get_user_storage_info(OWNER)

        args = mock_run.call_args[0]
        assert "zfs" in args
        assert "get" in args
        assert "-Hp" in args
        assert "quota,used,available" in args
        assert USER_DS in args

    async def test_failure_returns_error(self):
        mock_run = AsyncMock(return_value=fail("dataset not found"))

        with patch("agent.tools.zfs.run_command", mock_run):
            info = await get_user_storage_info(OWNER)

        assert info.success is False
        assert info.error is not None
        assert "dataset not found" in info.error
        assert info.quota == "unknown"
        assert info.used == "unknown"
        assert info.available == "unknown"

    async def test_message_includes_usage_summary(self):
        zfs_output = "quota\t10737418240\nused\t1073741824\navailable\t9663676416\n"
        mock_run = AsyncMock(return_value=ok(zfs_output))

        with patch("agent.tools.zfs.run_command", mock_run):
            info = await get_user_storage_info(OWNER)

        assert OWNER in info.message
        assert "used" in info.message.lower() or info.used in info.message
        assert "available" in info.message.lower() or info.available in info.message

    async def test_quota_none(self):
        """When quota is 'none' (unlimited), it passes through."""
        zfs_output = "quota\tnone\nused\t0\navailable\t0\n"
        mock_run = AsyncMock(return_value=ok(zfs_output))

        with patch("agent.tools.zfs.run_command", mock_run):
            info = await get_user_storage_info(OWNER)

        assert info.success is True
        assert info.quota == "none"

    async def test_large_values(self):
        """Handles terabyte-scale values."""
        tb = 1024 * 1024 * 1024 * 1024
        zfs_output = f"quota\t{2 * tb}\nused\t{tb}\navailable\t{tb}\n"
        mock_run = AsyncMock(return_value=ok(zfs_output))

        with patch("agent.tools.zfs.run_command", mock_run):
            info = await get_user_storage_info(OWNER)

        assert info.success is True
        assert "T" in info.quota
        assert "T" in info.used


# ── _apply_quota (via create_user_datasets integration) ───────────────────────


class TestApplyQuota:
    """Tests for quota application, exercised through create_user_datasets."""

    async def test_quota_set_command_format(self):
        """Verifies the exact zfs set command format."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),  # exists
                ok(),  # zfs set quota
            ]
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_user_datasets(OWNER)

        quota_call = mock_run.call_args_list[1]
        assert quota_call[0][0] == "zfs"
        assert quota_call[0][1] == "set"
        assert quota_call[0][2] == f"quota={DEFAULT_QUOTA}"
        assert quota_call[0][3] == USER_DS

    async def test_none_quota_disables_limit(self):
        """Setting quota to 'none' disables the limit."""
        mock_run = AsyncMock(
            side_effect=[
                ok(USER_DS),
                ok(),  # quota
            ]
        )

        with (
            patch("agent.tools.zfs.get_settings", return_value=_mock_settings("none")),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        quota_call = mock_run.call_args_list[1]
        assert quota_call[0][2] == "quota=none"
