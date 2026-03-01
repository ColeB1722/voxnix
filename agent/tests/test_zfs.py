"""Tests for ZFS dataset management tools.

TDD — these tests define the contract for ZFS dataset operations.
All CLI calls are mocked; no real ZFS pool is required to run these tests.

The get_settings() call in create_user_datasets (for quota) is mocked via
a module-level autouse fixture so tests don't require env vars.

Test refactoring (#74): tests use command-matching dispatch functions instead
of ordered AsyncMock side_effect lists. This makes tests resilient to
call-order changes — adding an intermediate zfs check or reordering calls
within a function won't break unrelated tests.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.cli import CommandResult
from agent.tools.zfs import (
    ZfsQuotaInfo,
    ZfsResult,
    _container_dataset,
    _ensure_mounted,
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
DEFAULT_QUOTA = "10G"
DEFAULT_POOL = "tank"

USER_DS = f"{DEFAULT_POOL}/users/{OWNER}"
CONTAINERS_DS = f"{DEFAULT_POOL}/users/{OWNER}/containers"
CONTAINER_DS = f"{DEFAULT_POOL}/users/{OWNER}/containers/{CONTAINER}"
WORKSPACE_DS = f"{DEFAULT_POOL}/users/{OWNER}/containers/{CONTAINER}/workspace"
MOUNT_PATH = f"/{DEFAULT_POOL}/users/{OWNER}/containers/{CONTAINER}/workspace"

USER_MOUNT = f"/{DEFAULT_POOL}/users/{OWNER}"
CONTAINERS_MOUNT = f"/{DEFAULT_POOL}/users/{OWNER}/containers"
CONTAINER_MOUNT = f"/{DEFAULT_POOL}/users/{OWNER}/containers/{CONTAINER}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def ok(stdout: str = "") -> CommandResult:
    """Successful CLI result."""
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def fail(stderr: str = "error") -> CommandResult:
    """Failed CLI result."""
    return CommandResult(stdout="", stderr=stderr, returncode=1)


def _mock_settings(quota: str = DEFAULT_QUOTA, pool: str = DEFAULT_POOL) -> MagicMock:
    """Return a mock VoxnixSettings with the given zfs_user_quota and zfs_pool."""
    settings = MagicMock()
    settings.zfs_user_quota = quota
    settings.zfs_pool = pool
    return settings


@pytest.fixture(autouse=True)
def _mock_get_settings():
    """Mock get_settings() for all tests so no env vars are required.

    Tests that need a different quota or pool can patch again locally.
    """
    with patch(
        "agent.tools.zfs.get_settings", return_value=_mock_settings(DEFAULT_QUOTA, DEFAULT_POOL)
    ):
        yield


# ── Command-matching dispatch helpers ─────────────────────────────────────────
#
# Instead of ordered side_effect lists, tests build a dispatch function that
# inspects the command arguments and returns the appropriate ok()/fail().
# This makes tests resilient to call-order changes within the implementation.
#
# Usage:
#     mock_run = make_dispatch({
#         ("list", USER_DS): ok(USER_DS),        # zfs list → dataset exists
#         ("get", "mounted", USER_DS): ok("yes"), # zfs get mounted → yes
#         ("set", f"mountpoint={USER_MOUNT}"): ok(),
#         ("set", f"quota={DEFAULT_QUOTA}"): ok(),
#     })


def _match_key(args: tuple) -> tuple[str, ...] | None:
    """Extract a matching key from a zfs command invocation.

    Given a full command like ("zfs", "list", "-H", "-o", "name", "tank/users/123"),
    produces a series of candidate keys from most-specific to least-specific so
    the dispatch table can match on the relevant parts.

    Returns the first key that could be used, or None if not a zfs command.
    """
    if not args or args[0] != "zfs":
        return None
    # args[1] is the subcommand: list, create, set, get, mount, destroy
    return tuple(args[1:])


def make_dispatch(
    table: dict[tuple[str, ...], CommandResult],
    *,
    default: CommandResult | None = None,
) -> AsyncMock:  # noqa: C901
    """Build an AsyncMock whose side_effect dispatches by command arguments.

    The table maps key tuples to results. A key matches if all elements of the
    key appear in the command args (in order, but not necessarily contiguous).
    More specific keys (longer tuples) are tried first.

    Args:
        table: Mapping of match keys to CommandResult.
        default: Fallback result if no key matches. If None, raises AssertionError.

    Returns:
        AsyncMock suitable for patching run_command.
    """
    # Sort keys longest-first so more specific matches take priority.
    sorted_keys = sorted(list(table.keys()), key=lambda k: len(k), reverse=True)

    async def dispatch(*args: object, **kwargs: object) -> CommandResult:
        str_args = tuple(str(a) for a in args)
        for key in sorted_keys:
            # Check that all elements of the key appear in the args in order.
            idx = 0
            matched = True
            for element in key:
                found = False
                while idx < len(str_args):
                    if str_args[idx] == element:
                        idx += 1
                        found = True
                        break
                    idx += 1
                if not found:
                    matched = False
                    break
            if matched:
                return table[key]

        if default is not None:
            return default
        raise AssertionError(f"Unexpected command: {str_args!r}")

    mock = AsyncMock(side_effect=dispatch)
    return mock


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
        assert _user_dataset("999") == f"{DEFAULT_POOL}/users/999"

    def test_workspace_mount_path_format(self):
        """Mount path must start with /<pool>/users/ — matches storage.nix layout."""
        path = _workspace_mount_path("owner1", "ctr1")
        assert path.startswith(f"/{DEFAULT_POOL}/users/")
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


# ── _ensure_mounted ───────────────────────────────────────────────────────────


class TestEnsureMounted:
    """Tests for the mount-verification helper."""

    async def test_already_mounted_returns_success(self):
        """Dataset already mounted — no mount command issued."""
        mock_run = make_dispatch(
            {
                ("get", "mounted", USER_DS): ok("yes"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await _ensure_mounted(USER_DS)

        assert result.success is True
        assert "already mounted" in result.message
        # Only one call — the get check. No mount needed.
        assert mock_run.call_count == 1

    async def test_not_mounted_triggers_mount(self):
        """Dataset exists but not mounted — zfs mount is called."""
        mock_run = make_dispatch(
            {
                ("get", "mounted", USER_DS): ok("no"),
                ("mount", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await _ensure_mounted(USER_DS)

        assert result.success is True
        assert "Mounted" in result.message
        assert mock_run.call_count == 2

    async def test_mount_failure_returns_error(self):
        """Dataset not mounted and mount command fails — error propagated."""
        mock_run = make_dispatch(
            {
                ("get", "mounted", USER_DS): ok("no"),
                ("mount", USER_DS): fail("mount failed: directory is not empty"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await _ensure_mounted(USER_DS)

        assert result.success is False
        assert result.error is not None
        assert "mount failed" in result.error

    async def test_get_mounted_check_failure_returns_error(self):
        """If we can't even check mount state, return error."""
        mock_run = make_dispatch(
            {
                ("get", "mounted", USER_DS): fail("dataset does not exist"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await _ensure_mounted(USER_DS)

        assert result.success is False
        assert result.error is not None

    async def test_mount_failure_logs_to_logger(self, caplog):
        """Mount failure is logged via the standard logger."""
        mock_run = make_dispatch(
            {
                ("get", "mounted", USER_DS): ok("no"),
                ("mount", USER_DS): fail("permission denied"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await _ensure_mounted(USER_DS)

        assert any("_ensure_mounted failed" in r.message for r in caplog.records)


# ── _ensure_dataset (via create_container_dataset intermediates) ──────────────


class TestEnsureDataset:
    """Tests for _ensure_dataset, exercised indirectly through create_container_dataset.

    _ensure_dataset is a private function that handles intermediate datasets
    (containers/, containers/<name>/). We test its behavior by verifying the
    full container dataset creation path, which calls _ensure_dataset for each
    intermediate level.
    """

    async def test_existing_unmounted_dataset_gets_mounted(self):
        """An intermediate dataset that exists but is unmounted gets mounted."""
        mock_run = make_dispatch(
            {
                # create_user_datasets: user exists, mounted, quota ok
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # create_container_dataset: workspace doesn't exist
                ("list", WORKSPACE_DS): fail("not found"),
                # _ensure_dataset for containers/: exists but not mounted
                ("list", CONTAINERS_DS): ok(CONTAINERS_DS),
                ("get", "mounted", CONTAINERS_DS): ok("no"),
                ("mount", CONTAINERS_DS): ok(),
                # _ensure_dataset for containers/<name>: exists but not mounted
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("get", "mounted", CONTAINER_DS): ok("no"),
                ("mount", CONTAINER_DS): ok(),
                # workspace create
                ("create", WORKSPACE_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH

        # Verify mount was called for the two intermediate datasets.
        mount_calls = [c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "mount"]
        assert len(mount_calls) == 2

    async def test_existing_mounted_dataset_skips_mount(self):
        """An intermediate dataset that is already mounted skips the mount call."""
        mock_run = make_dispatch(
            {
                # create_user_datasets
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace doesn't exist
                ("list", WORKSPACE_DS): fail("not found"),
                # containers/ exists and is mounted
                ("list", CONTAINERS_DS): ok(CONTAINERS_DS),
                ("get", "mounted", CONTAINERS_DS): ok("yes"),
                # containers/<name> doesn't exist — create it
                ("list", CONTAINER_DS): fail("not found"),
                ("create", CONTAINER_DS): ok(),
                # workspace create
                ("create", WORKSPACE_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True

        # No mount calls — containers/ was already mounted, container_ds was freshly created.
        mount_calls = [c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "mount"]
        assert len(mount_calls) == 0


# ── create_user_datasets ──────────────────────────────────────────────────────


class TestCreateUserDatasets:
    async def test_creates_dataset_when_missing(self):
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("does not exist"),
                ("create", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert result.dataset == USER_DS

        # Verify create was called with explicit mountpoint.
        create_calls = [
            c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "create"
        ]
        assert len(create_calls) == 1
        create_args = create_calls[0][0]
        assert create_args[0] == "zfs"
        assert create_args[1] == "create"
        assert "-o" in create_args
        assert any("mountpoint=" in str(a) for a in create_args)
        assert create_args[-1] == USER_DS

    async def test_idempotent_when_exists_and_mounted(self):
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True
        assert "already exists" in result.message

    async def test_existing_unmounted_dataset_gets_mounted(self):
        """User dataset exists but is not mounted — mount is triggered."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("no"),
                ("mount", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True

        # Verify mount was called.
        mount_calls = [c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "mount"]
        assert len(mount_calls) == 1

    async def test_existing_unmounted_mount_failure_returns_error(self):
        """User dataset exists, unmounted, and mount fails — error returned."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("no"),
                ("mount", USER_DS): fail("mount failed"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is False
        assert "could not be mounted" in result.message

    async def test_create_failure_returns_error(self):
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): fail("permission denied"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is False
        assert result.error is not None
        assert "permission denied" in result.error

    async def test_create_failure_logs_to_logger(self, caplog):
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): fail("no space"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_user_datasets(OWNER)

        assert any("create_user_datasets failed" in r.message for r in caplog.records)

    async def test_uses_explicit_mountpoint_on_create(self):
        """Dataset is created with an explicit mountpoint so it auto-mounts."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_user_datasets(OWNER)

        create_calls = [
            c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "create"
        ]
        assert len(create_calls) == 1
        create_args = create_calls[0][0]
        assert "-o" in create_args
        assert any("mountpoint=" in str(a) for a in create_args)
        assert f"/tank/users/{OWNER}" in " ".join(str(a) for a in create_args)

    async def test_quota_applied_on_new_dataset(self):
        """Quota is applied after dataset creation."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True

        # Verify quota set was called.
        quota_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 3 and c[0][1] == "set" and "quota=" in str(c[0][2])
        ]
        assert len(quota_calls) == 1
        assert quota_calls[0][0] == ("zfs", "set", f"quota={DEFAULT_QUOTA}", USER_DS)

    async def test_quota_applied_on_existing_dataset(self):
        """Quota is reapplied to existing datasets (keeps config in sync)."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert result.success is True

        quota_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 3 and c[0][1] == "set" and "quota=" in str(c[0][2])
        ]
        assert len(quota_calls) == 1
        assert quota_calls[0][0] == ("zfs", "set", f"quota={DEFAULT_QUOTA}", USER_DS)

    async def test_custom_quota_from_settings(self):
        """Quota value comes from VoxnixSettings.zfs_user_quota."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): ok(),
                ("set", "quota=50G", USER_DS): ok(),
            }
        )

        with (
            patch("agent.tools.zfs.get_settings", return_value=_mock_settings("50G")),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_user_datasets(OWNER)

        quota_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 3 and c[0][1] == "set" and "quota=" in str(c[0][2])
        ]
        assert len(quota_calls) == 1
        assert quota_calls[0][0] == ("zfs", "set", "quota=50G", USER_DS)

    async def test_quota_failure_on_new_dataset_returns_failure(self, caplog):
        """Quota failure on a newly created dataset returns success=False."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): fail("invalid quota"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            result = await create_user_datasets(OWNER)

        assert result.success is False
        assert result.error is not None
        assert any("quota application failed" in r.message for r in caplog.records)

    async def test_quota_failure_on_existing_dataset_returns_failure(self, caplog):
        """Quota failure on an already-existing dataset returns success=False."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): fail("permission denied"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            result = await create_user_datasets(OWNER)

        assert result.success is False
        assert result.error is not None
        assert any("quota application failed" in r.message for r in caplog.records)

    async def test_quota_in_success_message(self):
        """Success message mentions the quota value."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): ok(),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_user_datasets(OWNER)

        assert DEFAULT_QUOTA in result.message


# ── create_container_dataset ──────────────────────────────────────────────────


class TestCreateContainerDataset:
    async def test_creates_workspace_dataset(self):
        """Full success path: user exists, workspace doesn't, create succeeds."""
        mock_run = make_dispatch(
            {
                # create_user_datasets: user exists and mounted
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace doesn't exist
                ("list", WORKSPACE_DS): fail("nope"),
                # intermediates don't exist — create them
                ("list", CONTAINERS_DS): fail("nope"),
                ("create", CONTAINERS_DS): ok(),
                ("list", CONTAINER_DS): fail("nope"),
                ("create", CONTAINER_DS): ok(),
                # workspace create
                ("create", WORKSPACE_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH
        assert result.dataset == WORKSPACE_DS

    async def test_idempotent_when_workspace_exists_and_mounted(self):
        """Workspace dataset already exists and is mounted — no create needed."""
        mock_run = make_dispatch(
            {
                # create_user_datasets
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace exists
                ("list", WORKSPACE_DS): ok(WORKSPACE_DS),
                ("set", f"mountpoint={MOUNT_PATH}", WORKSPACE_DS): ok(),
                ("get", "mounted", WORKSPACE_DS): ok("yes"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH
        assert "already exists" in result.message

    async def test_existing_unmounted_workspace_gets_mounted(self):
        """Workspace exists but isn't mounted — mount is triggered before returning."""
        mock_run = make_dispatch(
            {
                # create_user_datasets
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace exists but not mounted
                ("list", WORKSPACE_DS): ok(WORKSPACE_DS),
                ("set", f"mountpoint={MOUNT_PATH}", WORKSPACE_DS): ok(),
                ("get", "mounted", WORKSPACE_DS): ok("no"),
                ("mount", WORKSPACE_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.mount_path == MOUNT_PATH

        # Verify mount was called for the workspace.
        mount_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "mount" and WORKSPACE_DS in c[0]
        ]
        assert len(mount_calls) == 1

    async def test_existing_workspace_mount_failure_returns_error(self):
        """Workspace exists, not mounted, mount fails — error propagated."""
        mock_run = make_dispatch(
            {
                # create_user_datasets
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace exists but mount fails
                ("list", WORKSPACE_DS): ok(WORKSPACE_DS),
                ("set", f"mountpoint={MOUNT_PATH}", WORKSPACE_DS): ok(),
                ("get", "mounted", WORKSPACE_DS): ok("no"),
                ("mount", WORKSPACE_DS): fail("mount point busy"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert "could not be mounted" in result.message

    async def test_user_dataset_creation_failure_propagates(self):
        """If user dataset creation fails, container dataset creation aborts."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): fail("not found"),
                ("create", USER_DS): fail("permission denied"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.mount_path is None

    async def test_workspace_create_failure(self):
        """User exists, but workspace dataset creation fails."""
        mock_run = make_dispatch(
            {
                # create_user_datasets
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                # workspace doesn't exist
                ("list", WORKSPACE_DS): fail("nope"),
                # intermediates
                ("list", CONTAINERS_DS): fail("nope"),
                ("create", CONTAINERS_DS): ok(),
                ("list", CONTAINER_DS): fail("nope"),
                ("create", CONTAINER_DS): ok(),
                # workspace create fails
                ("create", WORKSPACE_DS): fail("quota exceeded"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await create_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.error is not None

    async def test_workspace_create_failure_logs_to_logger(self, caplog):
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                ("list", WORKSPACE_DS): fail("nope"),
                ("list", CONTAINERS_DS): fail("nope"),
                ("create", CONTAINERS_DS): ok(),
                ("list", CONTAINER_DS): fail("nope"),
                ("create", CONTAINER_DS): ok(),
                ("create", WORKSPACE_DS): fail("out of space"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await create_container_dataset(OWNER, CONTAINER)

        assert any("create_container_dataset failed" in r.message for r in caplog.records)

    async def test_creates_full_hierarchy_with_explicit_mountpoints(self):
        """Each dataset level is created with an explicit mountpoint."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                ("list", WORKSPACE_DS): fail("nope"),
                ("list", CONTAINERS_DS): fail("nope"),
                ("create", CONTAINERS_DS): ok(),
                ("list", CONTAINER_DS): fail("nope"),
                ("create", CONTAINER_DS): ok(),
                ("create", WORKSPACE_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_container_dataset(OWNER, CONTAINER)

        # Verify workspace create uses explicit mountpoint.
        workspace_creates = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "create" and WORKSPACE_DS in c[0]
        ]
        assert len(workspace_creates) == 1
        ws_args = workspace_creates[0][0]
        assert "-o" in ws_args
        # Mountpoint must equal the expected host path.
        mp_arg = next(a for a in ws_args if str(a).startswith("mountpoint="))
        assert mp_arg == f"mountpoint={MOUNT_PATH}"

    async def test_mount_path_matches_storage_layout(self):
        """Mount path must match the disko layout in storage.nix."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
                ("list", WORKSPACE_DS): fail("nope"),
                ("list", CONTAINERS_DS): fail("nope"),
                ("create", CONTAINERS_DS): ok(),
                ("list", CONTAINER_DS): fail("nope"),
                ("create", CONTAINER_DS): ok(),
                ("create", WORKSPACE_DS): ok(),
            }
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
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("destroy", CONTAINER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert result.dataset == CONTAINER_DS

    async def test_calls_zfs_destroy_recursive(self):
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("destroy", "-r", CONTAINER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await destroy_container_dataset(OWNER, CONTAINER)

        destroy_calls = [
            c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "destroy"
        ]
        assert len(destroy_calls) == 1
        assert destroy_calls[0][0] == ("zfs", "destroy", "-r", CONTAINER_DS)

    async def test_succeeds_when_dataset_does_not_exist(self):
        """No dataset to destroy — treat as success (already clean)."""
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): fail("does not exist"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is True
        assert "does not exist" in result.message
        # Only one call — the existence check. No destroy needed.
        assert mock_run.call_count == 1

    async def test_destroy_failure_returns_error(self):
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("destroy", CONTAINER_DS): fail("busy"),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        assert result.success is False
        assert result.error is not None
        assert "busy" in result.error

    async def test_destroy_failure_logs_to_logger(self, caplog):
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("destroy", CONTAINER_DS): fail("dataset is busy"),
            }
        )

        with (
            caplog.at_level(logging.ERROR, logger="agent.tools.zfs"),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            await destroy_container_dataset(OWNER, CONTAINER)

        assert any("destroy_container_dataset failed" in r.message for r in caplog.records)

    async def test_destroys_container_root_not_user_root(self):
        """Only the container subtree is destroyed, not the user root."""
        mock_run = make_dispatch(
            {
                ("list", CONTAINER_DS): ok(CONTAINER_DS),
                ("destroy", CONTAINER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            result = await destroy_container_dataset(OWNER, CONTAINER)

        # The destroyed dataset should be the container root, NOT the user root.
        assert result.dataset == CONTAINER_DS
        assert result.dataset != USER_DS

        # The destroy command should target the container dataset.
        destroy_calls = [
            c for c in mock_run.call_args_list if len(c[0]) >= 2 and c[0][1] == "destroy"
        ]
        assert len(destroy_calls) == 1
        assert CONTAINER in destroy_calls[0][0][-1]


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
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", f"quota={DEFAULT_QUOTA}", USER_DS): ok(),
            }
        )

        with patch("agent.tools.zfs.run_command", mock_run):
            await create_user_datasets(OWNER)

        quota_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 3 and c[0][1] == "set" and "quota=" in str(c[0][2])
        ]
        assert len(quota_calls) == 1
        assert quota_calls[0][0][0] == "zfs"
        assert quota_calls[0][0][1] == "set"
        assert quota_calls[0][0][2] == f"quota={DEFAULT_QUOTA}"
        assert quota_calls[0][0][3] == USER_DS

    async def test_none_quota_disables_limit(self):
        """Setting quota to 'none' disables the limit."""
        mock_run = make_dispatch(
            {
                ("list", USER_DS): ok(USER_DS),
                ("set", f"mountpoint={USER_MOUNT}", USER_DS): ok(),
                ("get", "mounted", USER_DS): ok("yes"),
                ("set", "quota=none", USER_DS): ok(),
            }
        )

        with (
            patch("agent.tools.zfs.get_settings", return_value=_mock_settings("none")),
            patch("agent.tools.zfs.run_command", mock_run),
        ):
            result = await create_user_datasets(OWNER)

        assert result.success is True

        quota_calls = [
            c
            for c in mock_run.call_args_list
            if len(c[0]) >= 3 and c[0][1] == "set" and "quota=" in str(c[0][2])
        ]
        assert len(quota_calls) == 1
        assert quota_calls[0][0][2] == "quota=none"
