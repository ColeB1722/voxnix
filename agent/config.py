"""Voxnix agent configuration — centralized environment variable management.

All runtime configuration is injected via agenix EnvironmentFile into the
agent's systemd service. This module is the single place where those variables
are declared, validated, and typed.

No module should call os.environ directly — import settings from here instead.

Usage:
    from agent.config import get_settings

    settings = get_settings()
    path = settings.voxnix_flake_path
    model = settings.llm_model_string

Environment variables (all injected by agenix at runtime):

  Required:
    VOXNIX_FLAKE_PATH     — Absolute path to the voxnix flake root on the host
                            (e.g. /var/lib/voxnix). Used by the Nix expression
                            generator to locate mkContainer.nix.
    LLM_PROVIDER          — LLM provider name as a pydantic-ai identifier
                            (e.g. "anthropic", "openai", "google-gla").
    LLM_MODEL             — Model name within the provider
                            (e.g. "claude-3-5-sonnet-latest", "gpt-4o").
    TELEGRAM_BOT_TOKEN    — Telegram Bot API token for the chat integration layer.
    <PROVIDER>_API_KEY    — Provider-specific API key, read directly by pydantic-ai.
                            e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY.
                            Not declared here — pydantic-ai reads these from the
                            environment based on the resolved provider.

  Optional:
    TELEGRAM_ALLOWED_CHAT_IDS — Comma-separated list of Telegram chat IDs authorized
                            to use the bot (e.g. "12345678,87654321"). When set, only
                            these users can interact with the bot — all others are
                            silently ignored. When unset or empty, the bot is LOCKED
                            (denies everyone) — fail-closed by design.
    LOGFIRE_TOKEN         — Logfire project token for observability.
                            If unset, logfire runs in local/dev mode (no remote export).
    ZFS_POOL              — ZFS pool name. Must match nix/host/storage.nix.
                            Default: "tank". Override when the appliance pool is named
                            differently (e.g. a second appliance with a different layout).
"""

from __future__ import annotations

import os
import re
import warnings
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default empty allowlist. Fail-closed: if you don't configure an allowlist,
# nobody gets in (both unset and explicitly empty deny everyone).
_EMPTY_CHAT_IDS: frozenset[int] = frozenset()

# Mapping from provider name → expected API key environment variable.
# Used to validate that the correct key is present at startup rather than
# surfacing a cryptic pydantic-ai error on the first LLM call.
# Providers mapped to None do not require an API key (e.g. ollama runs locally).
_PROVIDER_API_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google-gla": "GOOGLE_API_KEY",  # Google AI Studio
    "google-vertex": None,  # Uses Application Default Credentials (ADC)
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",  # Grok
    "cohere": "CO_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "bedrock": None,  # Uses AWS credentials chain
    "ollama": None,  # Local, no key
}


class VoxnixSettings(BaseSettings):
    """Centralized configuration for the voxnix agent.

    All fields are read from environment variables (injected by agenix).
    Field names map to env vars by uppercasing: llm_provider → LLM_PROVIDER.

    Instantiate via get_settings() to benefit from caching.
    """

    model_config = SettingsConfigDict(
        # Allow .env files in development — agenix EnvironmentFile takes precedence
        # in production since it sets actual env vars before the process starts.
        env_file=".env",
        env_file_encoding="utf-8",
        # case_sensitive=False (default) — pydantic-settings uppercases field names
        # when matching env vars, so voxnix_flake_path → VOXNIX_FLAKE_PATH.
        # Emit a clear error message for missing required fields.
        # Pydantic will list all missing vars in a single ValidationError.
    )

    # ── Infrastructure ──────────────────────────────────────────────────────

    voxnix_flake_path: str
    """Absolute path to the voxnix flake root (e.g. /var/lib/voxnix).
    Used by the Nix expression generator to locate mkContainer.nix."""

    # ── LLM provider ────────────────────────────────────────────────────────

    llm_provider: str
    """LLM provider identifier (e.g. "anthropic", "openai", "google-gla").
    Combined with llm_model to form the pydantic-ai model string."""

    llm_model: str
    """Model name within the provider (e.g. "claude-3-5-sonnet-latest")."""

    # ── Chat integration ─────────────────────────────────────────────────────

    telegram_bot_token: SecretStr
    """Telegram Bot API token. SecretStr prevents accidental logging."""

    telegram_allowed_chat_ids: frozenset[int] = _EMPTY_CHAT_IDS
    """Set of Telegram chat IDs authorized to interact with the bot.

    Parsed from a comma-separated env var: TELEGRAM_ALLOWED_CHAT_IDS=12345678,87654321

    Security model (fail-closed):
      - When set: only listed chat IDs can use the bot.
      - When empty or unset: ALL users are denied. The bot is locked by default.
        This prevents accidental exposure if the env var is forgotten during deploy.

    Add your own chat_id to the agenix secrets file (agent-env.age) to unlock.
    Find your chat_id by messaging @userinfobot on Telegram.

    See docs/architecture.md § Trust Model and issue #61.
    """

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def parse_allowed_chat_ids(cls, v: object) -> frozenset[int]:
        """Parse comma-separated chat IDs from the environment variable.

        Accepts:
          - A string like "12345678,87654321" (from env var)
          - An already-parsed frozenset/set/list of ints (from code)
          - Empty string / None → empty frozenset (fail-closed)

        Raises ValueError on non-integer tokens so typos surface at startup.
        """
        if v is None or v == "":
            return _EMPTY_CHAT_IDS
        if isinstance(v, (frozenset, set)):
            return frozenset(v)
        if isinstance(v, (list, tuple)):
            return frozenset(v)
        if isinstance(v, str):
            raw_ids = [token.strip() for token in v.split(",") if token.strip()]
            if not raw_ids:
                return _EMPTY_CHAT_IDS
            try:
                return frozenset(int(id_str) for id_str in raw_ids)
            except ValueError as e:
                msg = (
                    f"TELEGRAM_ALLOWED_CHAT_IDS contains non-integer value: {e}. "
                    "Expected comma-separated integers (e.g. '12345678,87654321')."
                )
                raise ValueError(msg) from e
        msg = f"Unexpected type for TELEGRAM_ALLOWED_CHAT_IDS: {type(v)}"
        raise TypeError(msg)

    def is_chat_authorized(self, chat_id: int) -> bool:
        """Check whether a Telegram chat ID is authorized to use the bot.

        Returns True only if the chat_id is in the allowlist.
        Returns False when the allowlist is empty (fail-closed).
        """
        return chat_id in self.telegram_allowed_chat_ids

    # ── Tailscale ────────────────────────────────────────────────────────────

    tailscale_auth_key: SecretStr | None = None
    """Reusable Tailscale auth key for container enrollment.

    Injected into containers that include the 'tailscale' module via
    environment.variables.TAILSCALE_AUTH_KEY in mkContainer.nix.

    Optional — the agent can function without Tailscale (containers are
    still reachable from the host LAN). But if a user requests the
    'tailscale' module and no auth key is configured, the agent returns
    a clear error rather than creating a broken container.

    Generate a reusable, ephemeral key from the Tailscale admin console.
    'Reusable' so multiple containers can use it; 'ephemeral' so devices
    auto-expire if the container is destroyed and never re-registered.

    See docs/architecture.md § Private access — Tailscale.
    """

    # ── ZFS ──────────────────────────────────────────────────────────────────

    zfs_pool: str = "tank"
    """ZFS pool name. Must match the pool defined in nix/host/storage.nix.

    Set via ZFS_POOL env var — the Nix host config is the single source of
    truth for the pool name. The Python default ("tank") is a fallback for
    local development only.

    Changing this without updating nix/host/storage.nix (or vice versa) will
    cause ZFS operations to target the wrong pool and fail with cryptic errors.
    """

    zfs_user_quota: str = "10G"
    """Per-user ZFS quota applied to tank/users/<chat_id>.

    Limits the total disk space consumed by all of a user's container
    workspaces combined. Individual containers share the user's quota —
    no per-container quota needed (the user allocates space across
    containers as they see fit).

    Applied by create_user_datasets() when provisioning a new user's
    dataset root. Idempotent — setting a quota on an existing dataset
    just updates the limit.

    Format: ZFS size string (e.g. "10G", "50G", "1T", "none" to disable).
    Configurable via ZFS_USER_QUOTA environment variable in agenix.
    Default: 10G — conservative for a shared homelab appliance.

    See docs/architecture.md § Trust Model — ZFS quotas per user.
    """

    @field_validator("zfs_user_quota")
    @classmethod
    def validate_quota_format(cls, v: str) -> str:
        """Validate that the quota string is a valid ZFS size or 'none'.

        ZFS accepts sizes like '10G', '512M', '1.5T', or 'none' to disable.
        Invalid values cause cryptic runtime failures in _apply_quota() —
        catching them here surfaces a clear error at startup. (Fixes #58.)
        """
        if v == "none":
            return v
        if not re.match(r"^\d+(\.\d+)?[KMGTP]$", v, re.IGNORECASE):
            msg = (
                f"Invalid ZFS quota format: '{v}'. "
                "Use a ZFS size string (e.g. '10G', '512M', '1T') or 'none' to disable."
            )
            raise ValueError(msg)
        return v

    # ── Observability ────────────────────────────────────────────────────────

    logfire_token: SecretStr | None = None
    """Logfire project token. Optional — if unset, logfire runs in local mode."""

    # ── Computed properties ──────────────────────────────────────────────────

    @property
    def llm_model_string(self) -> str:
        """PydanticAI model identifier string (e.g. "anthropic:claude-3-5-sonnet-latest")."""
        return f"{self.llm_provider}:{self.llm_model}"

    # ── Validators ───────────────────────────────────────────────────────────

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        known = set(_PROVIDER_API_KEY_ENV.keys())
        if v not in known:
            warnings.warn(
                f"Unknown LLM_PROVIDER '{v}'. API key validation will be skipped. "
                f"Known providers: {', '.join(sorted(known))}",
                stacklevel=2,
            )
        return v

    @model_validator(mode="after")
    def validate_provider_api_key(self) -> VoxnixSettings:
        """Check that the provider-specific API key is present in the environment.

        pydantic-ai reads these keys directly (e.g. ANTHROPIC_API_KEY), so they
        are not declared as fields here. This validator surfaces a clear error at
        startup rather than a cryptic failure on the first LLM call.
        """
        key_env_var = _PROVIDER_API_KEY_ENV.get(self.llm_provider)
        if key_env_var is not None and not os.environ.get(key_env_var):
            msg = (
                f"LLM_PROVIDER is '{self.llm_provider}' but {key_env_var} is not set. "
                f"Inject {key_env_var} via agenix or set it in your environment."
            )
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> VoxnixSettings:
    """Return the cached VoxnixSettings instance.

    Reads from environment on first call, then caches for the process lifetime.
    Call clear_settings_cache() in tests to reset between test cases.
    """
    return VoxnixSettings()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]  — BaseSettings reads from env


def clear_settings_cache() -> None:
    """Clear the settings cache.

    Use in tests that need to vary environment variables between cases:

        def test_something(monkeypatch):
            monkeypatch.setenv("LLM_PROVIDER", "openai")
            clear_settings_cache()
            settings = get_settings()
            ...
        # cache clears automatically via monkeypatch teardown if you
        # call clear_settings_cache() in a fixture autouse teardown.
    """
    get_settings.cache_clear()
