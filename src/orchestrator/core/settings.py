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

import ipaddress
import os
import warnings
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from apscheduler.triggers.cron import CronTrigger
from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SPIKE_F_CHUNK_CONCURRENCY = 32

# Lookup names whose raw rejected value must never reach a ValidationError's
# echoed `input_value` (and thus logs). A token-related validation failure is
# re-raised as a scrubbed ValueError in `__init__`. Matched per-`loc`-element,
# EXACTLY and case-insensitively (lowercased here) — NOT as a `"token"`
# substring (which would over-scrub a future non-secret field containing
# "token"). Crucially this must include EVERY validation_alias for the secret
# field: pydantic puts the matched alias in the error `loc`, so an `ORCH_TOKEN`
# env-var failure has `loc=('ORCH_TOKEN',)`, not `('orchestrator_token',)`. Keep
# in sync with the `orchestrator_token` AliasChoices below (review 2026-06-02).
_SECRET_FIELD_NAMES = frozenset({"orchestrator_token", "orch_token"})


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
    # NOTE: length constraint is enforced via _check_token_length (mode="after"),
    # not Field(min_length=32). Rationale: pydantic's core min_length echoes the
    # rejected raw string in ValidationError.input, which would leak a
    # candidate token to logs on a rotation-failure startup (SEV-2).
    # Running the check on the SecretStr object keeps the error payload redacted.
    orchestrator_token: SecretStr = Field(
        ...,
        validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token"),
    )
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8765, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)
    # Source-IP allowlist for LAN exposure. Empty => no extra sources beyond
    # loopback (and the SourceAllowlistMiddleware is a pure no-op). Comma-
    # separated IPs/CIDRs in env; NoDecode keeps pydantic-settings from trying
    # to JSON-decode the value before our before-validator splits it.
    allowed_source_ips: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Database & migrations --------------------------------------
    database_path: Path = Path("/var/lib/orchestrator/orchestrator.db")
    require_local_fs: Literal["strict", "warn", "off"] = "warn"

    # --- Platform session paths -------------------------------------
    steam_session_path: Path = Path("/var/lib/orchestrator/steam_session.json")
    epic_session_path: Path = Path("/var/lib/orchestrator/epic_session.json")

    # --- SteamPrefill delegation (re-architecture step 1) -----------
    # The orchestrator drives the host-installed SteamPrefill binary for Steam
    # prefill (modern persistent auth). Config dir holds account.config (auth),
    # selectedAppsToPrefill.json, successfullyDownloadedDepots.json.
    steam_prefill_binary: Path = Path("/SteamPrefill/SteamPrefill")
    steam_prefill_config_dir: Path = Path("/SteamPrefill/Config")

    # --- Data-plane agent (re-architecture step 2) ------------------
    # The data plane (chunk-pull + cache disk-stat + SteamPrefill runner) runs
    # as a separate HTTP service (the agent) on the lancache host. agent_enabled
    # routes the control-plane handlers through it; OFF keeps the in-process path
    # (zero behavior change). agent_base_url is loopback while co-located (step
    # 2) and becomes the LXC->host LAN address in step 4.
    agent_enabled: bool = False
    agent_base_url: str = "http://127.0.0.1:8780"
    agent_bind_host: str = Field(default="127.0.0.1", min_length=1)
    agent_bind_port: int = Field(default=8780, ge=1, le=65535)

    # --- Steam worker deletion (re-arch step 3) ---------------------
    # The agent reads SteamPrefill's manifest cache (mounted read-only from the
    # host's /root/.cache/SteamPrefill) to source chunk SHAs for validate.
    steam_manifest_cache_dir: Path = Path("/steamprefill-cache")
    # Flag: route Steam validate through the agent's /v1/steam/validate (parses
    # SteamPrefill manifests) instead of the legacy worker manifest_expand.
    steam_validate_via_agent: bool = False

    # --- Lancache cache topology ------------------------------------
    lancache_nginx_cache_path: Path = Path("/data/cache/cache/")
    cache_slice_size_bytes: int = Field(default=10_485_760, gt=0)
    cache_levels: str = Field(default="2:2", pattern=r"^\d+(:\d+)*$")
    # F7: nginx $cacheidentifier for Steam traffic (the lancache map sets
    # this to the literal "steam"). Part of the cache-key md5 input.
    steam_cache_identifier: str = "steam"
    # Prefill (F5) chunk-download concurrency. Pre-staged generic name; F5
    # uses it as the per-game parallel-chunk cap.
    chunk_concurrency: int = Field(default=32, ge=1, le=256)

    # --- F5 Steam prefill -------------------------------------------
    # Prefill streams depot chunks THROUGH the lancache so they get cached
    # under the key F7 validates (spike A5). Target the lancache; override
    # the Host + UA so nginx classifies the request as `steam`.
    lancache_base_url: str = "http://127.0.0.1"
    steam_cdn_host: str = "lancache.steamcontent.com"
    prefill_user_agent: str = "Valve/Steam HTTP Client 1.0"
    prefill_chunk_timeout_sec: float = Field(default=10.0, gt=0.0, le=120.0)
    prefill_chunk_max_attempts: int = Field(default=3, ge=1, le=10)

    # --- Lancache self-test (ID2) -----------------------------------
    # Heartbeat URL — `http://<lancache>/lancache-heartbeat` returns
    # 200 + identifier string when the lancache nginx is up. Default
    # uses the compose service name; deployments where the orchestrator
    # is co-resident with lancache (DNS bypass) should set this to
    # `http://127.0.0.1/lancache-heartbeat` or similar.
    lancache_heartbeat_url: str = Field(
        default="http://lancache/lancache-heartbeat",
        min_length=1,
        max_length=2048,
    )
    lancache_probe_timeout_sec: float = Field(default=5.0, gt=0.0, le=60.0)
    lancache_probe_cache_ttl_sec: float = Field(default=30.0, ge=0.0, le=600.0)

    # --- Scheduler (F12) --------------------------------------------
    # APScheduler AsyncIOScheduler integration. Disable for diagnostic
    # / dev: /health.scheduler_running surfaces False and the endpoint
    # returns 503 per JQ3.
    scheduler_enabled: bool = Field(default=True)
    # Interval between library_sync schedule fires; FRD says 6h default.
    # Bounded [60 s, 86400 s] so operators can tune for testing without
    # accidentally setting a pathological value.
    scheduler_library_sync_interval_sec: int = Field(default=21600, ge=60, le=86400)
    # F13 — scheduled validation sweep.
    validation_sweep_enabled: bool = True
    validation_sweep_cron: str = "0 3 * * 0"  # 5-field cron (min hour dom mon dow), UTC
    # F8: the scheduled prefill driver runs on the library-sync interval.
    scheduled_prefill_enabled: bool = True
    sweep_batch_size: int = Field(default=10, ge=1)

    # --- Misc --------------------------------------------------------
    manifest_size_cap_bytes: int = Field(default=134_217_728, gt=0)
    epic_refresh_buffer_sec: int = Field(default=600, ge=0)
    steam_upstream_silent_days: int = Field(default=15, ge=1)

    # --- Epic (F6) ---------------------------------------------------
    # Refresh token persists to epic_session_path (JSON, like steam_session_path).
    epic_token_url: str = Field(
        default="https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
    )
    epic_library_url: str = Field(
        default="https://library-service.live.use1a.on.epicgames.com/library/api/public/items"
    )
    epic_manifest_url_template: str = Field(
        default=(
            "https://launcher-public-service-prod06.ol.epicgames.com"
            "/launcher/api/public/assets/v2/platform/{platform}"
            "/namespace/{namespace}/catalogItem/{catalog_item_id}"
            "/app/{app_name}/label/{label}"
        )
    )
    # Public legendary launcher client creds — the well-known EGS launcher app
    # credentials used by every Epic CLI client; NOT operator secrets.
    epic_client_id: str = Field(default="34a02cf8f4414e29b15921876da36f9a")
    epic_client_secret: str = Field(default="daafbccc737745039dffe53d94fc76cf")
    epic_user_agent: str = Field(default="EpicGamesLauncher/11.0.1-14907503+++Portal+Release-Live")
    epic_manifest_label: str = Field(default="Live")
    epic_platform: str = Field(default="Windows")

    # --- DB pool & SQLite tuning (BL4) ---
    pool_readers: int = Field(default=8, ge=1, le=32)
    # Floor of 100 ms (SEV-4, code review 2026-06-02): busy_timeout=0 disables
    # SQLite's busy wait entirely, so any write contention surfaces immediately
    # as WriteConflictError instead of being absorbed by a short retry window.
    pool_busy_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    # SEV-2 fix (code review 2026-06-02): bound how long a read waits for a
    # free reader connection. If the reader pool is exhausted (e.g. slots lost
    # to failed replacements after disk I/O errors), reads raise PoolError
    # rather than blocking forever.
    pool_reader_acquire_timeout_sec: float = Field(default=30.0, gt=0.0, le=300.0)
    db_cache_size_kib: int = Field(default=16384, ge=1024, le=1048576)
    db_mmap_size_bytes: int = Field(default=268_435_456, ge=0, le=17_179_869_184)
    db_journal_size_limit_bytes: int = Field(default=67_108_864, ge=1_048_576, le=1_073_741_824)
    # Issue #40: low-free-space threshold for the DB-volume health check. When
    # `free_pct` drops below this, health_check surfaces `low_space=True` and
    # the pool emits a rate-limited `pool.disk_low` WARNING.
    pool_disk_low_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    # Issue #41: emit `pool.query_completed` at DEBUG after each successful
    # read/write helper call (with template-only SQL + param shape, never raw
    # values). Opt-in to avoid log volume at INFO/WARN deployments.
    pool_query_log_completed: bool = Field(default=False)

    # --- Steam worker (BL10 / F1) ---
    steam_worker_python_path: Path = Path("/opt/orchestrator/venv-steam-worker/bin/python")
    steam_worker_ipc_timeout_sec: int = Field(default=30, ge=1, le=600)
    # Issue #109: library.enumerate + (future) manifest.fetch handle real
    # Steam libraries that take minutes to enumerate. Default budgets a
    # 5-minute ceiling — well above the empirical worst case for hundreds
    # of batched get_product_info calls, but bounded so a wedged worker
    # still surfaces an error eventually.
    steam_worker_library_enumerate_timeout_sec: int = Field(default=300, ge=30, le=3600)
    # BL12 manifest fetcher: each fetch can issue multiple
    # ContentServerDirectory.GetManifestRequestCode + manifest downloads
    # against Steam's CDN. Big games with 50+ depots can take 1-3 min
    # serially; budget 5 min by default.
    steam_worker_manifest_fetch_timeout_sec: int = Field(default=300, ge=30, le=3600)
    # F7 validator: manifest.expand just zstd-decompresses + protobuf-parses
    # a stored BLOB (offline, no network). Fast, but big manifests can take
    # a few seconds; budget 2 min.
    steam_worker_manifest_expand_timeout_sec: int = Field(default=120, ge=30, le=600)
    steam_worker_max_restart_attempts: int = Field(default=3, ge=0, le=10)
    steam_session_dir: Path = Path("/var/lib/orchestrator/steam_session")
    jobs_worker_poll_interval_sec: float = Field(default=1.0, gt=0.0, le=60.0)
    # Per-job wall-clock budget. A handler that wedges past this is cancelled and
    # the job marked failed, so it can't hold the single worker loop forever
    # (self-heals without a process restart). Generous by default — a prefill of
    # a large game through lancache is long but resumes from cache on retry, so a
    # rare false timeout is not catastrophic. `0` disables the budget.
    job_max_runtime_sec: float = Field(default=21600.0, ge=0.0)

    @field_validator("cors_origins")
    @classmethod
    def _reject_empty_cors_origin(cls, v: list[str]) -> list[str]:
        """Every origin in the list must be a non-empty string."""
        if any(not o for o in v):
            raise ValueError("cors_origins must not contain empty strings")
        return v

    @field_validator("allowed_source_ips", mode="before")
    @classmethod
    def _split_allowed_source_ips(cls, v: Any) -> Any:
        """Accept a comma-separated env string or a real list. A bare env
        string like '10.100.23.102,10.0.0.0/24' is split + trimmed; empty
        segments are dropped."""
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("allowed_source_ips", mode="after")
    @classmethod
    def _validate_allowed_source_ips(cls, v: list[str]) -> list[str]:
        """Each entry must parse as an IP network (a bare IP becomes /32 or
        /128). Fail fast at construction on a malformed entry."""
        for entry in v:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as e:
                raise ValueError(f"invalid allowed_source_ips entry {entry!r}: {e}") from e
        return v

    @cached_property
    def allowed_source_networks(
        self,
    ) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Parsed allowlist networks, consumed by SourceAllowlistMiddleware.
        Entries are pre-validated by _validate_allowed_source_ips."""
        return [ipaddress.ip_network(e, strict=False) for e in self.allowed_source_ips]

    @field_validator("cache_levels", mode="after")
    @classmethod
    def _validate_cache_levels(cls, v: str) -> str:
        """F7 bug A: the regex allows widths like '0' or '99' or many levels
        whose total exceeds the 32-char md5 hex. Those silently produce wrong
        cache paths (validator reports everything missing). Reject at load.
        """
        widths = [int(x) for x in v.split(":")]
        if any(w < 1 for w in widths):
            raise ValueError(f"cache_levels widths must each be >= 1: {v!r}")
        if sum(widths) > 32:
            raise ValueError(
                f"cache_levels widths sum to {sum(widths)} > 32 (md5 hex length): {v!r}"
            )
        return v

    @field_validator("validation_sweep_cron", mode="after")
    @classmethod
    def _validate_sweep_cron(cls, v: str) -> str:
        """Fail-fast on a malformed cron (IS2) by constructing the trigger now."""
        try:
            CronTrigger.from_crontab(v)
        except Exception as e:  # apscheduler raises ValueError on bad expressions
            raise ValueError(f"invalid validation_sweep_cron {v!r}: {e}") from e
        return v

    @field_validator("orchestrator_token", mode="before")
    @classmethod
    def _strip_token(cls, v: Any) -> Any:
        """Strip whitespace before the length check. Bible §7.3."""
        if isinstance(v, SecretStr):
            return SecretStr(v.get_secret_value().strip())
        if isinstance(v, str):
            return v.strip()
        return v  # pragma: no cover — defensive fallthrough for unexpected input types

    @field_validator("orchestrator_token", mode="after")
    @classmethod
    def _check_token_length(cls, v: SecretStr) -> SecretStr:
        """Enforce minimum length on the SecretStr object (not the raw
        string). pydantic's error payload carries the SecretStr's
        redacted form, not the raw rejected value. Bible §7.3.

        V-5 hardening: also reject embedded control characters (after
        whitespace stripping). NUL, CR, LF, TAB, VT, FF in the token
        body could enable downstream issues — log-line truncation,
        HTTP-header injection if echoed, parser confusion. Trailing
        whitespace is still stripped by `_strip_token` (Bible §7.3
        contract); only embedded control chars in the surviving body
        are rejected.
        """
        raw = v.get_secret_value()
        if len(raw) < 32:
            raise ValueError(
                "orchestrator_token must be at least 32 characters after whitespace stripping"
            )
        # ASCII control range 0x00-0x1F + 0x7F (DEL). After stripping,
        # NO control chars should appear in the body.
        for ch in raw:
            if ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    "orchestrator_token must not contain control characters "
                    "(NUL/CR/LF/TAB/etc.) after whitespace stripping"
                )
        return v

    def __init__(__pydantic_self__, **kwargs: Any) -> None:  # noqa: N805 — matches BaseSettings convention to avoid field-name collisions
        """Wrap pydantic's ValidationError so that errors involving
        the orchestrator_token field don't echo the raw rejected value
        in the exception's input_value field (SEV-2). pydantic's core
        unconditionally tracks the input into ValidationError; we
        intercept at construction and re-raise token-related failures
        as ValueError with a scrubbed message. Non-token field errors
        propagate as the original ValidationError unchanged.
        """
        try:
            super().__init__(**kwargs)
        except ValidationError as e:
            # SEV-4 (code review 2026-06-02): match each loc element EXACTLY
            # (case-insensitively) against the secret field's lookup names rather
            # than a `"token" in loc` substring. The substring over-matched a
            # future non-secret field containing "token"; a naive exact match on
            # only the field name would UNDER-match the `ORCH_TOKEN` alias (whose
            # loc is the alias, not the field name) and leak the raw token.
            token_errors = [
                err
                for err in e.errors()
                if any(str(loc).lower() in _SECRET_FIELD_NAMES for loc in err.get("loc", ()))
            ]
            if token_errors:
                msgs = "; ".join(err.get("msg", "unknown error") for err in token_errors)
                raise ValueError(f"orchestrator_token validation failed: {msgs}") from None
            raise

    def __reduce__(self) -> Any:
        """Block pickling. SecretStr's default __reduce__ serialises
        the raw secret in _secret_value, so pickling a Settings
        instance writes the cleartext token into the pickle stream —
        which any future code path that pickles Settings (multiprocessing
        task args, on-disk cache, Celery) would persist to an
        attacker-readable location. Explicit TypeError forces callers
        to re-read config from source via get_settings().
        """
        raise TypeError("Settings is not pickle-safe — re-read via get_settings()")

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
            # case_sensitive=False means a lowercase `orch_token` env var also
            # shadows the secrets file and takes precedence — match os.environ
            # keys case-insensitively so the warning fires for it too, not only
            # the conventional uppercase form (audit 2026-06-09).
            env_has_token = any(k.upper() == "ORCH_TOKEN" for k in os.environ)
            if env_has_token and secret_file.is_file():
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

        # 5. Over-provisioned reader pool (BL4)
        if self.pool_readers > self.chunk_concurrency:
            log.warning(
                "config.pool_readers_over_provisioned",
                pool_readers=self.pool_readers,
                chunk_concurrency=self.chunk_concurrency,
                hint="pool_readers > chunk_concurrency means readers will idle; "
                "consider reducing pool_readers",
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Lazy singleton accessor. First call constructs; subsequent
    calls return the cached instance. Tests clear via
    `get_settings.cache_clear()` in the `_isolated_env` autouse fixture.
    """
    # The Docker-secrets dir is expected to be absent off the deployment host
    # (e.g. a dev laptop or the CLI). pydantic-settings warns about the missing
    # `secrets_dir` on every construction — noise that makes an operator think
    # something is wrong. Suppress just that warning (UAT-11 S11-E-07); a genuine
    # missing token still surfaces as a clean error.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r'directory ".*" does not exist')
        return Settings()


def reload_settings() -> Settings:
    """Force a fresh instantiation — primarily for tests or for a
    future SIGHUP-style config reload. Clears the `get_settings`
    cache and returns a freshly-built instance.
    """
    get_settings.cache_clear()
    return get_settings()
