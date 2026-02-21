"""Tests for Nix expression generation from ContainerSpec.

TDD — these tests define the contract for the generator that produces
Nix expressions consumed by extra-container. The generator is pure
Python string manipulation; tests verify structure and correctness
of the output without evaluating Nix.
"""

import pytest
from pydantic import ValidationError

from agent.config import clear_settings_cache
from agent.nix_gen.generator import generate_container_expr
from agent.nix_gen.models import ContainerSpec

FAKE_FLAKE_PATH = "/var/lib/voxnix"

# Minimal env vars required to instantiate VoxnixSettings.
# Used in tests that exercise the settings-based flake path resolution.
_BASE_ENV = {
    "VOXNIX_FLAKE_PATH": FAKE_FLAKE_PATH,
    "LLM_PROVIDER": "anthropic",
    "LLM_MODEL": "claude-3-5-sonnet-latest",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "ANTHROPIC_API_KEY": "test-api-key",
}


@pytest.fixture(autouse=True)
def _clear_settings():
    """Clear settings cache before and after each test."""
    clear_settings_cache()
    yield
    clear_settings_cache()


def make_spec(
    name: str = "dev-abc",
    owner: str = "chat_123",
    modules: list[str] | None = None,
) -> ContainerSpec:
    return ContainerSpec(
        name=name,
        owner=owner,
        modules=modules if modules is not None else ["git", "fish"],
    )


class TestGenerateContainerExpr:
    """generate_container_expr produces a Nix expression for extra-container."""

    def test_returns_string(self):
        expr = generate_container_expr(make_spec(), flake_path=FAKE_FLAKE_PATH)
        assert isinstance(expr, str)

    def test_imports_mk_container_from_flake_path(self):
        expr = generate_container_expr(make_spec(), flake_path=FAKE_FLAKE_PATH)
        assert f"{FAKE_FLAKE_PATH}/nix/mkContainer.nix" in expr

    def test_container_name_in_expr(self):
        spec = make_spec(name="my-dev")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '"my-dev"' in expr

    def test_owner_in_expr(self):
        spec = make_spec(owner="987654321")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '"987654321"' in expr

    def test_single_module_in_expr(self):
        spec = make_spec(modules=["git"])
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '"git"' in expr

    def test_multiple_modules_in_expr(self):
        spec = make_spec(modules=["git", "fish", "workspace"])
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '"git"' in expr
        assert '"fish"' in expr
        assert '"workspace"' in expr

    def test_modules_formatted_as_nix_list(self):
        """Modules must appear as a Nix list: [ "git" "fish" ]"""
        spec = make_spec(modules=["git", "fish"])
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '[ "git" "fish" ]' in expr

    def test_single_module_formatted_as_nix_list(self):
        spec = make_spec(modules=["workspace"])
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '[ "workspace" ]' in expr

    def test_expr_is_valid_nix_structure(self):
        """Expression should be a let...in block calling mkContainer."""
        expr = generate_container_expr(make_spec(), flake_path=FAKE_FLAKE_PATH)
        assert "let" in expr
        assert "in" in expr
        assert "mkContainer" in expr

    def test_different_flake_paths(self):
        expr = generate_container_expr(make_spec(), flake_path="/opt/voxnix")
        assert "/opt/voxnix/nix/mkContainer.nix" in expr


class TestFlakePathResolution:
    """Flake path can be passed explicitly or read from VOXNIX_FLAKE_PATH via settings."""

    def test_explicit_flake_path_used(self):
        expr = generate_container_expr(make_spec(), flake_path="/explicit/path")
        assert "/explicit/path/nix/mkContainer.nix" in expr

    def test_env_var_used_when_no_explicit_path(self, monkeypatch):
        for k, v in _BASE_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("VOXNIX_FLAKE_PATH", "/from/env")

        expr = generate_container_expr(make_spec())
        assert "/from/env/nix/mkContainer.nix" in expr

    def test_explicit_path_overrides_env_var(self, monkeypatch):
        for k, v in _BASE_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("VOXNIX_FLAKE_PATH", "/from/env")

        expr = generate_container_expr(make_spec(), flake_path="/explicit/path")
        assert "/explicit/path/nix/mkContainer.nix" in expr
        assert "/from/env" not in expr

    def test_missing_path_raises(self, monkeypatch):
        """Missing VOXNIX_FLAKE_PATH raises a pydantic ValidationError from settings."""
        for k, v in _BASE_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("VOXNIX_FLAKE_PATH", raising=False)

        with pytest.raises(ValidationError, match="voxnix_flake_path"):
            generate_container_expr(make_spec())
