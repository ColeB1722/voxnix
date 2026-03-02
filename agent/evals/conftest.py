"""Shared fixtures for the voxnix eval suite.

Responsibilities:
  - Gate all evals on API key availability (skip gracefully when missing)
  - Mock all CLI infrastructure so evals never touch a real appliance
  - Mock module discovery to return a realistic module list
  - Provide settings overrides so VoxnixSettings validates without prod secrets
  - Detect available provider API keys for cross-model parametrization

Design decisions:
  - The LLM call itself is NOT mocked — that's the entire point of evals.
    We test a real model's tool selection and response quality.
  - All infrastructure below the agent (run_command, discover_modules, ZFS)
    is fully mocked. The agent thinks it's talking to a real appliance.
  - Mock responses are plausible — the agent should be able to reason about
    them and compose a realistic response for the user.
  - Settings are patched to use LLM_PROVIDER / LLM_MODEL from the environment
    (same vars as prod), with dummy values for non-LLM fields.

See docs/handoff.md § Agent evals (#51) for architecture decisions.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from agent.config import clear_settings_cache
from agent.tools.cli import CommandResult

# ── Marker ─────────────────────────────────────────────────────────────────────

# Each test module sets its own pytestmark = pytest.mark.evals (conftest's
# pytestmark doesn't propagate to test modules). Combined with
# addopts = "-m 'not evals'" in pyproject.toml, `pytest agent/` skips
# evals by default; `pytest -m evals -o addopts=` runs them.

# ── Judge model credentials ────────────────────────────────────────────────────
# deepeval's default judge (gpt-4.1) uses the OpenAI client internally.
# The OpenAI SDK reads OPENAI_API_KEY and OPENAI_BASE_URL from the env
# (note: NOT OPENAI_API_BASE — that's a legacy LangChain convention).
# When the eval provider is OpenRouter, we alias the OpenRouter key so
# deepeval routes through OpenRouter's OpenAI-compatible endpoint —
# one key in .env.eval, no duplication.
_OPENAI_KEY_IS_ALIAS = False
if os.environ.get("LLM_PROVIDER") == "openrouter":
    _or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if _or_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = _or_key
        os.environ.setdefault("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
        _OPENAI_KEY_IS_ALIAS = True

# ── Credential detection ───────────────────────────────────────────────────────

# Provider → (env var for API key, PydanticAI model string prefix)
_PROVIDER_MODELS: dict[str, tuple[str, str]] = {
    "openrouter": ("OPENROUTER_API_KEY", "openrouter:anthropic/claude-haiku-4"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic:claude-haiku-4"),
    "openai": ("OPENAI_API_KEY", "openai:gpt-4.1-mini"),
    "google-gla": ("GOOGLE_API_KEY", "google-gla:gemini-2.0-flash"),
    "xai": ("XAI_API_KEY", "xai:grok-3-mini"),
    "groq": ("GROQ_API_KEY", "groq:llama-3.3-70b-versatile"),
}

# The primary provider is whatever LLM_PROVIDER is set to.
_PRIMARY_PROVIDER = os.environ.get("LLM_PROVIDER", "")
_PRIMARY_KEY_VAR = (
    dict(_PROVIDER_MODELS).get(_PRIMARY_PROVIDER, ("", ""))[0] if _PRIMARY_PROVIDER else ""
)


def _has_primary_credentials() -> bool:
    """Check if the primary eval LLM credentials are configured."""
    if not _PRIMARY_PROVIDER:
        return False
    key_var = _PROVIDER_MODELS.get(_PRIMARY_PROVIDER, (None, None))[0]
    if key_var is None:
        # Provider doesn't need a key (e.g. ollama)
        return bool(os.environ.get("LLM_MODEL", ""))
    return bool(os.environ.get(key_var, ""))


def available_models() -> list[str]:
    """Return PydanticAI model strings for all providers with *real* keys set.

    Used by cross-model parametrized tests. If only one key is configured,
    returns a single-element list. If five keys are set, returns five models.

    Skips the ``openai`` entry when OPENAI_API_KEY was set by the
    OpenRouter alias above — that's the same key routed through
    OpenRouter, not a real OpenAI account. Including it would produce
    false cross-model coverage (both entries hit the same backend).
    """
    models: list[str] = []
    for provider, (key_var, model_string) in _PROVIDER_MODELS.items():
        if provider == "openai" and _OPENAI_KEY_IS_ALIAS:
            continue
        if os.environ.get(key_var, ""):
            models.append(model_string)
    return models


# Skip the entire evals directory if no LLM credentials are available.
# This makes `just test` (which now includes the marker exclusion) safe,
# and also handles CI runs where secrets aren't configured.
if not _has_primary_credentials():
    # Module-level skip — pytest collects the file but skips all tests.
    pytest.skip(
        "No eval LLM credentials configured (set LLM_PROVIDER + matching API key in .env.eval)",
        allow_module_level=True,
    )

# ── Fake module list ───────────────────────────────────────────────────────────

FAKE_MODULES: list[str] = ["fish", "git", "tailscale", "workspace"]

# ── Canned CLI responses ───────────────────────────────────────────────────────
# These simulate what the agent's tools would get back from run_command().
# They need to be plausible enough that the agent can reason about them.


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Simulate a successful CLI command."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=0)


def _fail(stdout: str = "", stderr: str = "command failed") -> CommandResult:
    """Simulate a failed CLI command."""
    return CommandResult(stdout=stdout, stderr=stderr, returncode=1)


# machinectl list with a running "dev" container
_MACHINECTL_LIST_JSON = json.dumps(
    [
        {
            "machine": "dev",
            "class": "container",
            "service": "nspawn",
            "state": "running",
            "os": "nixos",
            "version": "25.05",
            "addresses": "10.100.0.101\n",
        }
    ]
)

# nixos-container list
_NIXOS_CONTAINER_LIST = "dev"

# Tailscale status inside container
_TAILSCALE_STATUS = (
    "100.83.13.65  dev  tagged-devices  linux  -\n"
    "# Health check:\n"
    "#     - not connected to home DERP region 1"
)

# VOXNIX_OWNER echo from inside container
_OWNER_ECHO = "test-owner"

# VOXNIX_MODULES echo from inside container
_MODULES_ECHO = "fish,git,tailscale,workspace"

# ZFS list output
_ZFS_LIST = (
    "tank/users/test-owner/containers/dev/workspace  "
    "128K  9.87G  128K  /tank/users/test-owner/containers/dev/workspace"
)

# Extra-container create success output
_EXTRA_CONTAINER_CREATE = "Installing containers: dev\n[1 Created]"

# Host health check responses
_WHICH_EXTRA_CONTAINER = "/nix/store/abc-extra-container/bin/extra-container"
_ZFS_VERSION = "zfs-2.2.4-1\nzfs-kmod-2.2.4-1"

# systemctl status
_SERVICE_STATUS = (
    "● voxnix-agent.service - Voxnix orchestrator agent (Telegram bot)\n"
    "     Loaded: loaded\n"
    "     Active: active (running) since Mon 2025-01-01 00:00:00 UTC; 1h ago\n"
    "   Main PID: 1234 (python)\n"
)

# Container status via machinectl
_MACHINECTL_STATUS_DEV = (
    "dev\n"
    "        Since: Mon 2025-01-01 00:00:00 UTC; 1h ago\n"
    "       Leader: 5678 (init)\n"
    "      Service: nspawn\n"
    "        State: running\n"
    "         Unit: container@dev.service\n"
)

# Tailscale IP query
_TAILSCALE_IP = "100.83.13.65"

# Tailscale status JSON for hostname extraction
_TAILSCALE_STATUS_JSON = json.dumps({"Self": {"HostName": "dev"}})

# Container uptime (machinectl show timestamp)
_CONTAINER_TIMESTAMP = "Timestamp=Mon 2025-01-01 00:00:00 UTC"


def _dispatch_command(*args: str, **kwargs) -> CommandResult:
    """Route mocked CLI calls to plausible canned responses.

    This is the core of the eval mock layer. Every tool in the agent
    ultimately calls run_command() — this function intercepts those calls
    and returns canned responses based on the command and arguments.

    The responses are intentionally realistic so the agent can reason
    about them and compose useful output for the user.
    """
    cmd = args[0] if args else ""
    all_args = " ".join(args)

    # ── machinectl ─────────────────────────────────────────────────────
    if cmd == "machinectl":
        if "list" in args:
            return _ok(_MACHINECTL_LIST_JSON)
        if "status" in args:
            return _ok(_MACHINECTL_STATUS_DEV)
        if "show" in args:
            return _ok(_CONTAINER_TIMESTAMP)
        return _ok()

    # ── nixos-container ────────────────────────────────────────────────
    if cmd == "nixos-container":
        if "list" in args:
            return _ok(_NIXOS_CONTAINER_LIST)
        if "run" in args:
            # Route based on what's being run inside the container
            if "VOXNIX_OWNER" in all_args:
                return _ok(_OWNER_ECHO)
            if "VOXNIX_MODULES" in all_args:
                return _ok(_MODULES_ECHO)
            if "tailscale" in all_args and "status" in all_args:
                return _ok(_TAILSCALE_STATUS)
            if "tailscale" in all_args and "ip" in all_args:
                return _ok(_TAILSCALE_IP)
            if "tailscale" in all_args and "logout" in all_args:
                return _ok()
            if "tailscale" in all_args and "json" in all_args:
                return _ok(_TAILSCALE_STATUS_JSON)
            return _ok()
        if "start" in args:
            return _ok()
        if "stop" in args:
            return _ok()
        return _ok()

    # ── extra-container ────────────────────────────────────────────────
    if cmd == "extra-container":
        if "create" in args:
            return _ok(_EXTRA_CONTAINER_CREATE)
        if "destroy" in args:
            return _ok()
        if "list" in args:
            return _ok("dev")
        return _ok()

    # ── nix ────────────────────────────────────────────────────────────
    if cmd == "nix":
        if "eval" in args:
            return _ok(json.dumps(FAKE_MODULES))
        return _ok()

    # ── zfs ────────────────────────────────────────────────────────────
    if cmd == "zfs":
        if "version" in args:
            return _ok(_ZFS_VERSION)
        if "list" in args:
            return _ok(_ZFS_LIST)
        if "create" in args:
            return _ok()
        if "set" in args:
            return _ok()
        if "get" in args:
            return _ok("tank/users/test-owner\tquota\t10G\tlocal")
        if "destroy" in args:
            return _ok()
        return _ok()

    # ── which ──────────────────────────────────────────────────────────
    if cmd == "which":
        if "extra-container" in all_args:
            return _ok(_WHICH_EXTRA_CONTAINER)
        return _ok(f"/usr/bin/{args[1]}" if len(args) > 1 else "")

    # ── systemctl ──────────────────────────────────────────────────────
    if cmd == "systemctl":
        if "status" in args:
            return _ok(_SERVICE_STATUS)
        if "list-unit-files" in args:
            return _ok("container@.service  enabled  enabled")
        return _ok()

    # ── journalctl ─────────────────────────────────────────────────────
    if cmd == "journalctl":
        return _ok("Jan 01 00:00:00 dev systemd[1]: Started container.\n" * 5)

    # ── tailscale (host-level) ─────────────────────────────────────────
    if cmd == "tailscale":
        return _ok(_TAILSCALE_STATUS)

    # ── Fallback ───────────────────────────────────────────────────────
    return _ok()


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_infrastructure(monkeypatch):
    """Mock all CLI calls and module discovery for evals.

    This is autouse — every eval test gets mocked infrastructure automatically.
    The LLM call itself is NOT mocked (that's the point of evals).
    """
    # Mock run_command in every module that imported it via
    # `from agent.tools.cli import run_command`. Patching the source
    # alone doesn't propagate — `from X import Y` copies the reference.
    mock_run = AsyncMock(side_effect=_dispatch_command)

    with (
        patch("agent.tools.cli.run_command", mock_run),
        patch("agent.tools.containers.run_command", mock_run),
        patch("agent.tools.zfs.run_command", mock_run),
        patch("agent.tools.workloads.run_command", mock_run),
        patch("agent.tools.diagnostics.run_command", mock_run),
        patch("agent.tools.query.run_command", mock_run),
        patch("agent.nix_gen.discovery.run_nix_eval", new_callable=AsyncMock) as mock_nix_eval,
        patch("agent.nix_gen.discovery._cache", FAKE_MODULES),
        # Mock the generator so create_container doesn't try to pipe nix expressions
        patch("agent.tools.containers.generate_container_expr", return_value="/* mock nix */"),
    ):
        mock_nix_eval.return_value = _ok(json.dumps(FAKE_MODULES))
        yield mock_run


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    """Ensure VoxnixSettings validates with eval-appropriate values.

    LLM_PROVIDER and LLM_MODEL come from the real environment (.env.eval).
    Non-LLM fields get dummy values so VoxnixSettings doesn't fail validation.
    """
    # Set dummy values for required non-LLM settings if not already set
    defaults = {
        "VOXNIX_FLAKE_PATH": "/tmp/voxnix-eval",
        "TELEGRAM_BOT_TOKEN": "000000000:fake-token-for-evals",
    }
    for key, value in defaults.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)

    # Clear the settings cache so each test gets fresh settings
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def eval_owner() -> str:
    """The test owner identity used in eval runs."""
    return "test-owner"


@pytest.fixture
def eval_model() -> str:
    """The PydanticAI model string for the primary eval model.

    Reads from LLM_PROVIDER / LLM_MODEL (same as prod config).
    """
    from agent.config import get_settings

    return get_settings().llm_model_string
