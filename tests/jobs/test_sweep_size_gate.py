"""#380 — a sweep must not START a game it cannot finish before the hard cap.

The #311 deadline only stops games from starting after `budget - margin`. On
2026-10-31 sweep 47123 started ARK (369,317 chunks) 5m52s before that line; its
validation needed longer than the 30 min that remained, so the worker's hard
cancel failed the job and the next scheduled sweep was dedup-skipped.

The size gate defers a game whose expected validation (its worst recent run, or
its size at a conservative rate when it has none) does not fit before the cap.
A deferred game is neither validated nor stamped, so the next run takes it first.

Clock and budget are chosen so the #311 margin never fires here: every decision
these tests observe is the size gate's.
"""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import ValidationResult

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32

# Budget 20000 s, margin 1800 s: the #311 deadline is at t=18200. The first game
# takes the clock to t=17000, leaving 3000 s before the cap. That window is where
# the size gate, and only the size gate, decides.
_BUDGET = 20000.0
_MARGIN = 1800.0
_FIRST_GAME_SECONDS = 17000.0


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


async def _game(
    pool,
    app_id: str,
    size_bytes: int | None,
    history_seconds: float | None,
    *,
    outcome: str = "cached",
    chunks: int = 1,
    age_days: int = 1,
) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, size_bytes) "
        "VALUES ('steam', ?, ?, 1, 'up_to_date', ?)",
        (app_id, app_id, size_bytes),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id = ?", (app_id,))
    game_id = int(row["id"])
    if history_seconds is not None:
        cached = chunks if outcome == "cached" else 0
        await pool.execute_write(
            "INSERT INTO validation_history (game_id, manifest_version, started_at, "
            "finished_at, method, chunks_total, chunks_cached, chunks_missing, outcome) "
            "VALUES (?, '1', datetime('now', '-' || ? || ' days'), "
            "datetime('now', '-' || ? || ' days', '+' || ? || ' seconds'), "
            "'disk_stat', ?, ?, ?, ?)",
            (
                game_id,
                age_days,
                age_days,
                int(history_seconds),
                chunks,
                cached,
                chunks - cached,
                outcome,
            ),
        )
    return game_id


async def _run(
    pool,
    monkeypatch,
    *,
    budget: float = _BUDGET,
    margin: float = _MARGIN,
    self_test_seconds: float = 0.0,
) -> tuple[list[int], object]:
    """Run one sweep at batch size 1. The validator self-test takes
    `self_test_seconds`; the first game validated then advances the clock by
    17000 s; every later one takes no clock time. Returns the validated ids."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    clock = _Clock()

    async def _healthy(settings, *, agent_client=None):
        clock.t += self_test_seconds
        return True

    monkeypatch.setattr(sweep_mod, "validator_self_test", _healthy)
    settings = Settings(
        orchestrator_token=VALID_TOKEN,
        sweep_batch_size=1,
        job_max_runtime_sec=budget,
        sweep_deadline_margin_sec=margin,
    )
    monkeypatch.setattr(sweep_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(sweep_mod, "monotonic", clock.monotonic)

    validated: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings_):
        if not validated:
            clock.t += _FIRST_GAME_SECONDS
        validated.append(game_id)
        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr(sweep_mod, "validate_one_game", fake_validate_one)
    summary = await sweep_handler(
        {"id": 1, "kind": "sweep", "payload": None}, Deps(pool=pool, agent_client=object())
    )
    return validated, summary


async def test_a_long_game_that_cannot_finish_before_the_cap_is_not_started(pool, monkeypatch):
    first = await _game(pool, "first", 1_000_000_000, 100)
    # Worst recent run 2400 s; padded it needs more than the 3000 s that remain.
    long_game = await _game(pool, "long", 400_000_000_000, 2400)

    validated, summary = await _run(pool, monkeypatch)

    assert validated == [first], f"the long game was started at t=17000: {validated}"
    row = await pool.read_one(
        "SELECT last_measure_attempt_at FROM games WHERE id = ?", (long_game,)
    )
    assert row["last_measure_attempt_at"] is None, (
        "a deferred game must stay unstamped so the next run takes it first"
    )
    assert summary is not None and "partial" in summary.msg.lower()


async def test_a_short_game_still_starts_after_a_long_one_is_deferred(pool, monkeypatch):
    first = await _game(pool, "first", 1_000_000_000, 100)
    await _game(pool, "long", 400_000_000_000, 2400)
    short = await _game(pool, "short", 1_000_000_000, 100)

    validated, _ = await _run(pool, monkeypatch)

    assert validated == [first, short], (
        f"only the long game should be held back; validated {validated}"
    )


async def test_a_game_with_no_history_is_estimated_from_its_size(pool, monkeypatch):
    first = await _game(pool, "first", 1_000_000_000, 100)
    # 200 GB at the conservative rate is ~55 min: it cannot fit in 3000 s.
    unmeasured = await _game(pool, "unmeasured", 200_000_000_000, None)

    validated, _ = await _run(pool, monkeypatch)

    assert unmeasured not in validated, "a game with no history must not bypass the gate"
    assert validated == [first]


async def test_an_estimate_bigger_than_a_whole_run_still_starts_at_the_beginning(pool, monkeypatch):
    # Worst run 30000 s exceeds the whole 20000 s budget. Capping the estimate is
    # what keeps such a game from being deferred forever.
    giant = await _game(pool, "giant", 900_000_000_000, 30000)

    validated, _ = await _run(pool, monkeypatch)

    assert validated == [giant]


async def test_history_alone_defers_a_small_game_that_ran_long(pool, monkeypatch):
    """Most Steam rows carry no size_bytes, so history is often the only signal.
    1 GB at the fallback rate is ~16 s; only its 2400 s run can defer it."""
    first = await _game(pool, "first", 1_000_000_000, 100)
    slow = await _game(pool, "slow", 1_000_000_000, 2400)

    validated, _ = await _run(pool, monkeypatch)

    assert slow not in validated, "its 2400 s worst run must defer it at t=17000"
    assert validated == [first]


async def test_history_older_than_the_window_is_ignored(pool, monkeypatch):
    first = await _game(pool, "first", 1_000_000_000, 100)
    stale = await _game(pool, "stale", 1_000_000_000, 2400, age_days=31)

    validated, _ = await _run(pool, monkeypatch)

    assert validated == [first, stale]


async def test_an_error_row_does_not_stand_in_for_a_size(pool, monkeypatch):
    """An error run measured nothing (chunks_total=0) and can take ~1 s. If it
    counted as history, a 200 GB game would be costed at 1.25 s and started."""
    first = await _game(pool, "first", 1_000_000_000, 100)
    big = await _game(pool, "big", 200_000_000_000, 1, outcome="error", chunks=0)

    validated, _ = await _run(pool, monkeypatch)

    assert big not in validated, "an error row must not override the size estimate"
    assert validated == [first]


async def test_a_fast_run_on_a_large_manifest_is_floored_by_its_chunk_count(pool, monkeypatch):
    """A Steam game with no size whose only history is a quick run (e.g. while it
    was missing from the cache) still has a known chunk count. ARK's 369,317
    chunks cost ~63 min at the p95 per-chunk rate; 60 s of history must not win."""
    first = await _game(pool, "first", 1_000_000_000, 100)
    ark = await _game(pool, "ark", None, 60, outcome="missing", chunks=369_317)

    validated, _ = await _run(pool, monkeypatch)

    assert ark not in validated, "its chunk count alone must defer it at t=17000"
    assert validated == [first]


async def test_a_small_margin_does_not_defer_a_capped_game_at_the_start(pool, monkeypatch):
    """With a margin under the gate's own buffer, the estimate cap used to equal
    the whole window to the hard line, so any time spent before the check
    deferred the game at the start of every run: a permanent green no-op."""
    giant = await _game(pool, "giant", 900_000_000_000, 30000)

    validated, summary = await _run(pool, monkeypatch, margin=100.0, self_test_seconds=60.0)

    assert validated == [giant], f"the capped game must still start: {summary}"


async def test_the_hard_line_counts_from_the_start_of_the_handler(pool, monkeypatch):
    """The worker's timer starts before the handler; the validator self-test (an
    agent RPC) runs inside it. Measuring the line from after the self-test puts
    it later than the real cap."""
    first = await _game(pool, "first", 1_000_000_000, 100)
    # needed 2750 s. At t=17300 (300 s self-test + 17000) it fits a line drawn
    # from after the self-test (20180) but not the real one (19880).
    late = await _game(pool, "late", 1_000_000_000, 2200)

    validated, _ = await _run(pool, monkeypatch, self_test_seconds=300.0)

    assert late not in validated, "the line must be measured from the handler's start"
    assert validated == [first]


async def test_no_budget_means_no_gate(pool, monkeypatch):
    first = await _game(pool, "first", 1_000_000_000, 100)
    long_game = await _game(pool, "long", 400_000_000_000, 2400)

    validated, _ = await _run(pool, monkeypatch, budget=0.0)

    assert validated == [first, long_game]
