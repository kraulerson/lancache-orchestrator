"""Typed application configuration for the lancache orchestrator.

Every Milestone B+ feature reads config through this module via
`get_settings()`. Fields are loaded in default pydantic-settings order:
init kwargs > env vars > .env file > /run/secrets files > defaults.

The single SecretStr field (`orchestrator_token`) supports two
lookup names via AliasChoices: `ORCH_TOKEN` (env var) and
`orchestrator_token` (secrets-file name, matching Bible §7.3).

See docs/superpowers/specs/2026-04-23-id4-settings-module-design.md
for the full design rationale.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SPIKE_F_CHUNK_CONCURRENCY = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core / API --------------------------------------------------
    orchestrator_token: SecretStr = Field(
        ...,
        validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token"),
        min_length=32,
    )
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8765, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Database & migrations --------------------------------------
    database_path: Path = Path("/var/lib/orchestrator/orchestrator.db")
    require_local_fs: Literal["strict", "warn", "off"] = "warn"

    # --- Platform session paths -------------------------------------
    steam_session_path: Path = Path("/var/lib/orchestrator/steam_session.json")
    epic_session_path: Path = Path("/var/lib/orchestrator/epic_session.json")

    # --- Lancache cache topology ------------------------------------
    lancache_nginx_cache_path: Path = Path("/data/cache/cache/")
    cache_slice_size_bytes: int = Field(default=10_485_760, gt=0)
    cache_levels: str = Field(default="2:2", pattern=r"^\d+(:\d+)*$")
    chunk_concurrency: int = Field(default=32, ge=1, le=256)

    # --- Misc --------------------------------------------------------
    manifest_size_cap_bytes: int = Field(default=134_217_728, gt=0)
    epic_refresh_buffer_sec: int = Field(default=600, ge=0)
    steam_upstream_silent_days: int = Field(default=15, ge=1)

    @field_validator("cors_origins")
    @classmethod
    def _reject_empty_cors_origin(cls, v: list[str]) -> list[str]:
        """Every origin in the list must be a non-empty string."""
        if any(not o for o in v):
            raise ValueError("cors_origins must not contain empty strings")
        return v

    @field_validator("orchestrator_token", mode="before")
    @classmethod
    def _strip_token(cls, v: Any) -> Any:
        """Strip whitespace before min_length runs. Bible §7.3."""
        if isinstance(v, SecretStr):
            return SecretStr(v.get_secret_value().strip())
        if isinstance(v, str):
            return v.strip()
        return v  # pragma: no cover — defensive fallthrough for unexpected input types

    @model_validator(mode="after")
    def _emit_config_warnings(self) -> Settings:
        """Emit diagnostic WARNINGs for non-fatal but notable config
        states: secret shadowed by env, non-loopback api_host,
        wildcard CORS, over-Spike-F chunk concurrency.
        """
        log = structlog.get_logger(__name__)

        # 1. Shadow warning — env and secret-file both set.
        # secrets_dir's type in model_config is Path | Sequence[Path|str] | None;
        # this project configures it as a single str, so narrow via isinstance.
        secrets_dir = self.model_config.get("secrets_dir")
        if isinstance(secrets_dir, (str, Path)):
            secret_file = Path(secrets_dir) / "orchestrator_token"
            if "ORCH_TOKEN" in os.environ and secret_file.is_file():
                log.warning(
                    "config.secret_shadowed_by_env",
                    secret_file=str(secret_file),
                )

        # 2. Non-loopback host
        if self.api_host not in _LOOPBACK_HOSTS:
            log.warning(
                "config.api_bound_non_loopback",
                api_host=self.api_host,
            )

        # 3. Wildcard CORS
        if "*" in self.cors_origins:
            log.warning("config.cors_wildcard")

        # 4. Over-Spike-F concurrency
        if self.chunk_concurrency > _SPIKE_F_CHUNK_CONCURRENCY:
            log.warning(
                "config.chunk_concurrency_unvalidated",
                chunk_concurrency=self.chunk_concurrency,
                spike_f_validated_at=_SPIKE_F_CHUNK_CONCURRENCY,
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Lazy singleton accessor. First call constructs; subsequent
    calls return the cached instance. Tests clear via
    `get_settings.cache_clear()` in the `_isolated_env` autouse fixture.
    """
    return Settings()


def reload_settings() -> Settings:
    """Force a fresh instantiation — primarily for tests or for a
    future SIGHUP-style config reload. Clears the `get_settings`
    cache and returns a freshly-built instance.
    """
    get_settings.cache_clear()
    return get_settings()
