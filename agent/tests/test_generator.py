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
    workspace_path: str | None = None,
    tailscale_auth_key: str | None = None,
) -> ContainerSpec:
    return ContainerSpec(
        name=name,
        owner=owner,
        modules=modules if modules is not None else ["git", "fish"],
        workspace_path=workspace_path,
        tailscale_auth_key=tailscale_auth_key,
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

    def test_workspace_path_included_when_set(self):
        spec = make_spec(workspace_path="/tank/users/chat_123/containers/dev-abc/workspace")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "workspace" in expr
        assert '"/tank/users/chat_123/containers/dev-abc/workspace"' in expr

    def test_workspace_path_omitted_when_none(self):
        spec = make_spec(workspace_path=None)
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "workspace" not in expr

    def test_workspace_path_as_nix_string(self):
        """workspace_path should appear as a Nix string assignment."""
        spec = make_spec(workspace_path="/tank/users/123/containers/dev/workspace")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert 'workspace = "/tank/users/123/containers/dev/workspace";' in expr

    def test_workspace_path_with_special_chars_escaped(self):
        """Dollar signs in paths must be escaped for Nix."""
        spec = make_spec(workspace_path="/tank/users/$bad/workspace")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "\\$" in expr
        assert '"/$bad"' not in expr

    def test_tailscale_auth_key_included_when_set(self):
        spec = make_spec(tailscale_auth_key="tskey-auth-abc123")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "tailscaleAuthKey" in expr
        assert '"tskey-auth-abc123"' in expr

    def test_tailscale_auth_key_omitted_when_none(self):
        spec = make_spec(tailscale_auth_key=None)
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "tailscaleAuthKey" not in expr

    def test_tailscale_auth_key_as_nix_string(self):
        """tailscaleAuthKey should appear as a Nix string assignment."""
        spec = make_spec(tailscale_auth_key="tskey-auth-xyz789")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert 'tailscaleAuthKey = "tskey-auth-xyz789";' in expr

    def test_tailscale_auth_key_special_chars_escaped(self):
        """Auth keys with dollar signs must be escaped for Nix."""
        spec = make_spec(tailscale_auth_key="tskey-$pecial")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "\\$" in expr

    def test_both_workspace_and_tailscale_included(self):
        """Both optional fields should appear in the spec when set."""
        spec = make_spec(
            workspace_path="/tank/users/123/containers/dev/workspace",
            tailscale_auth_key="tskey-auth-both",
        )
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "workspace" in expr
        assert "tailscaleAuthKey" in expr
        assert '"/tank/users/123/containers/dev/workspace"' in expr
        assert '"tskey-auth-both"' in expr


class TestNixStringEscaping:
    """_nix_string must escape Nix special characters to produce valid syntax."""

    def test_double_quote_in_owner_escaped(self):
        spec = make_spec(owner='chat_"123"')
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '\\"' in expr
        assert '"chat_"123""' not in expr

    def test_backslash_in_owner_escaped(self):
        spec = make_spec(owner="chat\\123")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "\\\\" in expr

    def test_dollar_sign_in_owner_escaped(self):
        """$ must be escaped to prevent Nix string interpolation."""
        spec = make_spec(owner="chat_$USER")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "\\$" in expr
        assert '"chat_$USER"' not in expr

    def test_dollar_sign_in_name_escaped(self):
        # ContainerSpec validation rejects names with $, so test via owner
        spec = make_spec(owner="$INJECTED")
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert "\\$" in expr

    def test_clean_values_unaffected(self):
        """Normal alphanumeric values should not be modified."""
        spec = make_spec(name="dev-abc", owner="123456789", modules=["git", "fish"])
        expr = generate_container_expr(spec, flake_path=FAKE_FLAKE_PATH)
        assert '"dev-abc"' in expr
        assert '"123456789"' in expr


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
