"""Shared fixtures for the voxnix eval suite.

Responsibilities:
  - Gate all evals on API key availability (skip gracefully when missing)
  - Mock all CLI infrastructure so evals never touch a real appliance
  - Mock module discovery to return a realistic module list
  - Provide settings overrides so VoxnixSettings validates without prod secrets
  - Configure the judge model (gpt-4.1) credentials from available API keys

Design decisions:
  - The LLM call itself is NOT mocked — that's the entire point of evals.
    We test a real model's tool selection and response quality.
  - All infrastructure below the agent (run_command, discover_modules, ZFS)
    is fully mocked. The agent thinks it's talking to a real appliance.
  - Mock responses are plausible — the agent should be able to reason about
    them and compose a realistic response for the user.
  - Settings are patched to use LLM_PROVIDER / LLM_MODEL from the environment
    (same vars as prod), with dummy values for non-LLM fields.
  - One model at a time: LLM_PROVIDER / LLM_MODEL control which single model
    is tested. Defaults to openrouter / anthropic/claude-haiku-4.5 so CI only
    needs OPENROUTER_API_KEY to auth both the test model and the judge.
  - The judge model (deepeval's gpt-4.1) always routes through whichever
    OpenAI-compatible key is available (OPENAI_API_KEY or OPENROUTER_API_KEY).

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

# ── Test model defaults ────────────────────────────────────────────────────────
# LLM_PROVIDER and LLM_MODEL control which single model is evaluated.
# They default to OpenRouter + Haiku 4.5 so that CI only needs one secret
# (OPENROUTER_API_KEY) to auth both the test model and the judge.
# Override via .env.eval or CI env vars to test a different model.

os.environ.setdefault("LLM_PROVIDER", "openrouter")
os.environ.setdefault("LLM_MODEL", "anthropic/claude-haiku-4.5")

# ── Judge model credentials ────────────────────────────────────────────────────
# deepeval's judge (gpt-4.1) uses the OpenAI client internally, which reads
# OPENAI_API_KEY and OPENAI_BASE_URL from the environment.
#
# We auto-detect credentials in priority order:
#   1. OPENAI_API_KEY already set → use it directly (native OpenAI path)
#   2. OPENROUTER_API_KEY set     → alias it to OPENAI_API_KEY and point
#                                   OPENAI_BASE_URL at OpenRouter's
#                                   OpenAI-compatible endpoint
#
# This means setting just OPENROUTER_API_KEY in CI is sufficient: it
# authenticates both the test model (via LLM_PROVIDER=openrouter) and the
# judge (via the alias below). A real OPENAI_API_KEY takes priority if present.

_JUDGE_CREDENTIALS: list[tuple[str, str | None]] = [
    # (env_var_for_key, OPENAI_BASE_URL override or None for native)
    ("OPENAI_API_KEY", None),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
]

_judge_configured = False
for _key_var, _base_url in _JUDGE_CREDENTIALS:
    _key_value = os.environ.get(_key_var, "")
    if not _key_value:
        continue
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = _key_value
    if _base_url is not None and not os.environ.get("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = _base_url
    _judge_configured = True
    break

# ── Credential check ──────────────────────────────────────────────────────────
# The test model provider needs its own API key. Map provider → env var.

_PROVIDER_KEY_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google-gla": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
}

_PRIMARY_PROVIDER = os.environ.get("LLM_PROVIDER", "")


def _has_credentials() -> bool:
    """Check if the test model API key and judge credentials are configured."""
    if not _PRIMARY_PROVIDER:
        return False
    key_var = _PROVIDER_KEY_VAR.get(_PRIMARY_PROVIDER)
    if key_var is None:
        # Unknown provider — assume it doesn't need a key (e.g. ollama)
        return _judge_configured
    return bool(os.environ.get(key_var, "")) and _judge_configured


# Skip the entire evals directory if credentials are missing.
# This makes `just test` safe, and handles CI runs without secrets.
if not _has_credentials():
    pytest.skip(
        "Eval credentials missing — need an API key for both the test model "
        f"({_PRIMARY_PROVIDER or 'unset'}) and the judge (OPENAI_API_KEY or "
        "OPENROUTER_API_KEY). See .env.eval.example.",
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
