"""A purge records what the disk holds afterwards, not what the agent said it did.

#310 (SEV-2). ``purge_handler`` read ``files_failed`` and never acted on it: it
unconditionally recorded ``partial`` and wrote a ``chunks_cached = 0`` observation,
whatever the agent reported. The agent returns HTTP 200 with
``{"deleted": 0, "failed": N}`` when every unlink fails.

The trigger is not hypothetical. Twice in this project the agent has come back as
uid 1000 instead of ``0:0``; every unlink then returns EACCES, **the cache on disk
is completely intact**, and the orchestrator writes ``validation_failed`` over it.
Because the write was ``commanded``, the circuit breaker — built for exactly "the
agent is lying about what it read" — was skipped by construction, so a bulk purge
of an intact library produced zero breaker signal and queued the whole set for
re-download.

Karl's decision on the issue: delete, then run a real validation and record *that*.
He rejected "any failure is total failure" (it leaves ``up_to_date`` over a
genuinely broken cache when 90% did delete) and "infer from the delete counts"
(still the agent's own report, not the disk).

The one thing the agent's counts are still allowed to decide is the fallback when
the post-purge validation itself fails — see the last two tests.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.purge import purge_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubAgent:
    """A purge agent that also answers the post-purge validation.

    ``validate`` is the state of the disk AFTER the deletes, which is the whole
    point: it is measured, not derived from ``steam_purge``'s return value.
    """

    def __init__(self, *, purge: dict, validate: dict | None = None):
        self._purge = purge
        self._validate = validate
        self.validate_calls = 0

    async def steam_purge(self, app_id: int):
        return self._purge

    async def epic_purge(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str):
        return self._purge

    async def steam_validate(self, app_id: int, chunk_count=None):
        self.validate_calls += 1
        if self._validate is None:
            raise AssertionError("post-purge validation was not expected here")
        return self._validate


def _job(game_id: int, platform: str = "steam") -> dict:
    return {"id": 1, "kind": "purge", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, app_id="440", status="up_to_date") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES ('steam', ?, 't', 1, ?)",
        (app_id, status),
    )
    row = await pool.read_one("SELECT id FROM games WHERE platform='steam' AND app_id=?", (app_id,))
    return int(row["id"])


async def _seed_validation(pool, game_id: int, *, total: int, cached: int) -> None:
    await pool.execute_write(
        "INSERT INTO validation_history (game_id, manifest_version, started_at, finished_at,"
        " method, chunks_total, chunks_cached, chunks_missing, outcome, error) "
        "VALUES (?, 'v1', '2026-08-01T00:00:00Z', '2026-08-01T00:01:00Z', 'disk_stat',"
        " ?, ?, ?, 'cached', NULL)",
        (game_id, total, cached, total - cached),
    )


def _agent_validate(*, total: int, cached: int, outcome: str) -> dict:
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": total - cached,
        "outcome": outcome,
        "versions": "v1",
    }


async def _latest_validation(pool, game_id: int):
    return await pool.read_one(
        "SELECT chunks_total, chunks_cached, chunks_missing, outcome "
        "FROM validation_history WHERE game_id=? ORDER BY id DESC LIMIT 1",
        (game_id,),
    )


async def _status(pool, game_id: int) -> str:
    row = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    return str(row["status"])


async def test_a_purge_whose_every_unlink_failed_leaves_the_intact_cache_alone(pool):
    """The uid-1000 case. Nothing was deleted, so nothing about the cache changed."""
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 0, "failed": 337, "bytes_freed": 0},
        validate=_agent_validate(total=337, cached=337, outcome="cached"),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert await _status(pool, game_id) == "up_to_date", (
        "every unlink failed and the files are all still there, but the orchestrator "
        "wrote validation_failed over an intact cache and queued it for re-download"
    )
    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 337, (
        "the recorded observation must be the measured disk, not a hard-coded zero"
    )
    assert agent.validate_calls == 1, "the status must come from a real measurement"


async def test_a_partly_failed_purge_records_what_survived_not_zero(pool):
    """90% deleted is not 100% deleted, and it is not 0% either. Go and look."""
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 300, "failed": 37, "bytes_freed": 900},
        validate=_agent_validate(total=337, cached=37, outcome="partial"),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert await _status(pool, game_id) == "validation_failed"
    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 37, (
        "37 chunks are still on disk; recording 0 tells the operator the purge finished"
    )
    assert latest["chunks_missing"] == 300


async def test_a_clean_purge_still_flags_the_game_for_re_prefill(pool):
    """The reversibility invariant (ADR-0015) must survive the rewrite.

    A measured-empty cache is ``not_downloaded``, which — like the old
    ``validation_failed`` — is in the set Epic's scheduled prefill selects on, and
    ``status_measured_at`` is stamped, which that query also requires.
    """
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 337, "failed": 0, "bytes_freed": 999},
        validate=_agent_validate(total=337, cached=0, outcome="missing"),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (game_id,))
    assert row["status"] in ("not_downloaded", "validation_failed")
    assert row["status_measured_at"] is not None, (
        "Epic's scheduled prefill requires status_measured_at IS NOT NULL; without it "
        "the purged game is never re-downloaded"
    )
    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0


async def test_the_purge_measurement_is_exempt_from_the_breaker(pool):
    """A bulk purge is deliberate, known cache loss — not evidence of a lying agent."""
    from orchestrator.core.settings import get_settings
    from orchestrator.jobs.measurement import record_measurement

    threshold = get_settings().measurement_breaker_threshold
    async with pool.write_transaction() as tx:
        for i in range(threshold + 1):
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', ?, 't', 1, 'up_to_date')",
                (f"lost{i}",),
            )
    for i in range(threshold + 1):
        row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (f"lost{i}",))
        try:
            await record_measurement(pool, int(row["id"]), "missing")
        except Exception:
            break  # the breaker has tripped, which is the setup this test needs

    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=10, cached=10)
    agent = _StubAgent(
        purge={"deleted": 10, "failed": 0, "bytes_freed": 5},
        validate=_agent_validate(total=10, cached=0, outcome="missing"),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert await _status(pool, game_id) == "not_downloaded", (
        "a tripped breaker vetoed the purge's record of files it had already deleted"
    )


async def test_the_purge_transition_row_is_marked_commanded(pool):
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=10, cached=10)

    agent = _StubAgent(
        purge={"deleted": 10, "failed": 0, "bytes_freed": 5},
        validate=_agent_validate(total=10, cached=0, outcome="missing"),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    rows = await pool.read_all(
        "SELECT downward, commanded FROM measurement_transitions WHERE game_id=?", (game_id,)
    )
    assert len(rows) == 1, "a purge is cache truth and belongs in the log"
    assert rows[0]["downward"] == 1
    assert rows[0]["commanded"] == 1, (
        "unmarked, a bulk purge arms the breaker against the next sweep"
    )


async def test_a_failed_post_purge_validation_after_real_deletes_still_clears_the_badge(pool):
    """The mirror defect must not come back.

    If the validation cannot run, the deletes have still happened. Leaving the
    pre-purge ``up_to_date`` in place would put a green badge over a cache this
    system itself emptied — the exact defect ``commanded`` was introduced to fix.
    With files confirmed deleted, fall back to the conservative ``partial``.
    """
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 337, "failed": 0, "bytes_freed": 999},
        validate={
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "cache not mounted",
        },
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert await _status(pool, game_id) == "validation_failed", (
        "337 files are gone and the measurement failed; the badge must not stay green"
    )

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_total"] == 337, (
        "#308 again, one file over: the failed validation appends its own 0-chunk row, "
        "so carrying the size from 'the newest row' takes it from the error and reports "
        f"a 0-chunk game. Got {latest['chunks_total']}."
    )
    assert latest["chunks_cached"] == 0


async def test_a_failed_post_purge_validation_that_deleted_nothing_changes_nothing(pool):
    """Nothing was deleted and nothing could be measured, so nothing is known."""
    game_id = await _seed_game(pool)
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 0, "failed": 337, "bytes_freed": 0},
        validate={
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "cache not mounted",
        },
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert await _status(pool, game_id) == "up_to_date", (
        "no delete succeeded and no measurement was made — inventing a status here is "
        "the #310 defect wearing a different hat"
    )
    row = await pool.read_one("SELECT last_measure_attempt_at FROM games WHERE id=?", (game_id,))
    assert row["last_measure_attempt_at"] is not None, "the attempt is still recorded"
