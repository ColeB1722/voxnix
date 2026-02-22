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
                            (e.g. "anthropic", "openai", "google").
    LLM_MODEL             — Model name within the provider
                            (e.g. "claude-3-5-sonnet-latest", "gpt-4o").
    TELEGRAM_BOT_TOKEN    — Telegram Bot API token for the chat integration layer.
    <PROVIDER>_API_KEY    — Provider-specific API key, read directly by pydantic-ai.
                            e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY.
                            Not declared here — pydantic-ai reads these from the
                            environment based on the resolved provider.

  Optional:
    LOGFIRE_TOKEN         — Logfire project token for observability.
                            If unset, logfire runs in local/dev mode (no remote export).
"""

from __future__ import annotations

import os
import warnings
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Mapping from provider name → expected API key environment variable.
# Used to validate that the correct key is present at startup rather than
# surfacing a cryptic pydantic-ai error on the first LLM call.
# Providers mapped to None do not require an API key (e.g. ollama runs locally).
_PROVIDER_API_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": None,
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
    """LLM provider identifier (e.g. "anthropic", "openai", "google").
    Combined with llm_model to form the pydantic-ai model string."""

    llm_model: str
    """Model name within the provider (e.g. "claude-3-5-sonnet-latest")."""

    # ── Chat integration ─────────────────────────────────────────────────────

    telegram_bot_token: SecretStr
    """Telegram Bot API token. SecretStr prevents accidental logging."""

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
