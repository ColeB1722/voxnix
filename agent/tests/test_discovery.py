"""Tests for module discovery — querying available modules from the Nix flake.

TDD — these tests define the contract for how the agent discovers
what modules are available without hardcoding them in Python.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.nix_gen.discovery import ModuleDiscoveryError, clear_cache, discover_modules


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Clear the module discovery cache before each test."""
    clear_cache()
    yield
    clear_cache()


class TestDiscoverModules:
    """Module discovery calls `nix eval .#lib.availableModules --json` and parses the result."""

    async def test_returns_list_of_module_names(self):
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["fish", "git", "workspace"])
        mock_result.stderr = ""

        with patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result):
            modules = await discover_modules()

        assert modules == ["fish", "git", "workspace"]

    async def test_returns_sorted_list(self):
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["workspace", "git", "fish"])
        mock_result.stderr = ""

        with patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result):
            modules = await discover_modules()

        assert modules == ["fish", "git", "workspace"]

    async def test_caches_result(self):
        """Discovery should only call nix eval once, then cache."""
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["git", "fish"])
        mock_result.stderr = ""

        with patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result) as mock_eval:
            first = await discover_modules(use_cache=True)
            second = await discover_modules(use_cache=True)

        assert first == second
        mock_eval.assert_called_once()

    async def test_cache_bypass(self):
        """use_cache=False should always call nix eval."""
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["git"])
        mock_result.stderr = ""

        with patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result) as mock_eval:
            await discover_modules(use_cache=False)
            await discover_modules(use_cache=False)

        assert mock_eval.call_count == 2

    async def test_nix_eval_failure_raises(self):
        mock_result = AsyncMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: flake 'git+file:///...' does not provide attribute"

        with (
            patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result),
            pytest.raises(ModuleDiscoveryError, match="nix eval"),
        ):
            await discover_modules(use_cache=False)

    async def test_invalid_json_raises(self):
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = "not valid json"
        mock_result.stderr = ""

        with (
            patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result),
            pytest.raises(ModuleDiscoveryError, match="parse"),
        ):
            await discover_modules(use_cache=False)

    async def test_unexpected_type_raises(self):
        """nix eval should return a list, not a string or object."""
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"git": "./modules/git.nix"})
        mock_result.stderr = ""

        with (
            patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result),
            pytest.raises(ModuleDiscoveryError, match="list"),
        ):
            await discover_modules(use_cache=False)

    async def test_empty_list_is_valid(self):
        """An empty module list is technically valid (no modules available)."""
        mock_result = AsyncMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([])
        mock_result.stderr = ""

        with patch("agent.nix_gen.discovery.run_nix_eval", return_value=mock_result):
            modules = await discover_modules(use_cache=False)

        assert modules == []
