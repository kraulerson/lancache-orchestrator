"""F7 — validate job handler.

Validates a Steam game's current depot manifests against the lancache
on-disk cache, records a `validation_history` row, and hands the outcome to
`orchestrator.jobs.measurement.record_measurement`, the single writer of
`games.status`. An `error` outcome (infra failure, e.g. cache not mounted) is
recorded as an attempt there and never clobbers the game's existing status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.measurement import record_measurement
from orchestrator.validator.disk_stat import ValidationResult, validate_game

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
    from orchestrator.db.pool import Pool
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_INSERT_VH = (
    "INSERT INTO validation_history "
    "(game_id, manifest_version, started_at, finished_at, method, "
    " chunks_total, chunks_cached, chunks_missing, outcome, error) "
    "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'disk_stat', ?, ?, ?, ?, ?)"
)


async def validate_one_game(
    pool: Pool, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate one game against the on-disk cache, record a validation_history
    row, and record the measurement. Shared by the validate job handler (F7) and
    the scheduled sweep (F13). Handles both steam and epic platforms — platform
    dispatch is done inside ``validate_game`` using the game's stored platform."""
    started_row = await pool.read_one("SELECT CURRENT_TIMESTAMP AS t")
    started_at = started_row["t"] if started_row is not None else None

    result = await validate_game(pool, deps, game_id, settings)

    await pool.execute_write(
        _INSERT_VH,
        (
            game_id,
            result.manifest_version,
            started_at,
            result.chunks_total,
            result.chunks_cached,
            result.chunks_missing,
            result.outcome,
            (result.error[:200] if result.error else None),
        ),
    )

    # Cache truth is written in exactly one place. An 'error' outcome records the
    # attempt only: the old "unstick a stranded 'downloading' by setting
    # status='failed'" write lived here and is deliberately gone — that is a job
    # outcome, not a cache measurement, and the startup job reaper (ID6) already
    # resolves stranded 'downloading' rows.
    await record_measurement(pool, game_id, result.outcome)
    return result


async def validate_handler(job: dict[str, Any], deps: Deps) -> None:
    """Validate one game's cached chunks (F7).

    Raises:
        ValueError — unsupported platform (not steam or epic) or unknown game.
    """
    platform = job.get("platform")
    if platform not in ("steam", "epic"):
        raise ValueError(f"validate supports steam+epic (got {platform!r})")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("validate job has no game_id")

    game = await deps.pool.read_one("SELECT id FROM games WHERE id=?", (game_id,))
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    # validate_game dispatches internally by the game's stored platform.

    job_id = job.get("id")
    settings = get_settings()
    _log.info("validate.started", job_id=job_id, game_id=game_id)

    result = await validate_one_game(deps.pool, deps, game_id, settings)

    _log.info(
        "validate.recorded",
        job_id=job_id,
        game_id=game_id,
        outcome=result.outcome,
        total=result.chunks_total,
        cached=result.chunks_cached,
        missing=result.chunks_missing,
    )
