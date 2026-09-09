"""Findings from the adversarial review of PR #303, each pinned by a test.

The recalibration in #303 was right in direction and oversold in effect. An
adversarial reviewer substantiated four defects; every one is a case of a number
that was correct in isolation and wrong in the system it lives in — the same
failure the PR itself was written to fix, one layer up.

1. THE SWEEP IS ONE JOB, HARD-KILLED AT ``job_max_runtime_sec`` (21600s).
   Raising the per-call ceiling does nothing for it. At the measured 48-54
   chunks/sec a 6h window covers ~1.1M chunks; the library is 26.5M. Worse, the
   candidate query ordered by ``id``, so every sweep re-validated the same head
   and died before the tail — games past the ~1M-chunk mark were never reached,
   deterministically, forever.

2. THE PER-CALL BUDGET IGNORED THE SWEEP'S OWN CONCURRENCY. ``sweep_batch_size``
   defaulted to 10 concurrent validates, all funnelling into the agent's
   ``_CACHE_STAT_WORKERS = 2`` stat pool. The 48-54 measurement was taken
   sequentially, so under a sweep each call got roughly a tenth of it while its
   budget assumed the full rate.

3. STEAM WAS NEVER FIXED. ``steam_validate`` is called with no chunk count, so
   it keeps the flat 300s base — about 12,000-16,000 chunks at the measured rate.
   The #303 audit certified this as "backward-compatible: unaffected". It IS
   unchanged; on this hardware that means broken, and backward-compatible was
   the wrong test to apply.

4. THE TESTS DID NOT CONSTRAIN THE IMPLEMENTATION. Setting the rate to 1.0 and
   the ceiling to 86400 — absurd in both directions, and a ceiling ABOVE the job
   budget — left all 28 tests passing. Reproduced before writing this file.
"""

from __future__ import annotations

from orchestrator.clients.agent_client import (
    VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC,
    VALIDATE_TIMEOUT_CEILING_SEC,
)
from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers import sweep as sweep_mod
from orchestrator.validator.disk_stat import _CACHE_STAT_WORKERS

# Measured on the post-rebuild hardware, 2026-09-01, from validation_history.
MEASURED_CHUNKS_PER_SEC = 48.0


class TestTheCeilingSitsUnderTheBudgetThatActuallyKills:
    def test_the_ceiling_cannot_exceed_the_job_runtime_budget(self) -> None:
        """A validate ceiling above ``job_max_runtime_sec`` is a lie: the worker
        wraps every handler in ``asyncio.wait_for`` (worker.py:224), so the job is
        cancelled first. Worse than useless — cancellation raises CancelledError,
        which is a BaseException and bypasses the sweep's per-game ``except
        Exception`` isolation, so in-flight validates abort writing NO
        validation_history row. That is precisely the "cannot self-correct"
        mechanism #303 claims to remove."""
        budget = Settings(orchestrator_token="x" * 32).job_max_runtime_sec
        assert budget >= VALIDATE_TIMEOUT_CEILING_SEC, (
            f"ceiling {VALIDATE_TIMEOUT_CEILING_SEC}s exceeds the job budget "
            f"{budget}s — the job is killed before the HTTP call gives up"
        )


class TestTheAssumedRateIsPinnedToSomethingReal:
    def test_the_rate_is_within_a_plausible_band_of_the_measurement(self) -> None:
        """The old value implied ~461/sec against a real 48-54 and nothing caught
        it. A band is what makes that catchable: too fast reintroduces the cliff,
        absurdly slow hands every game a budget past the job runtime and hides
        genuine agent wedges. Widen it deliberately with a measurement, never to
        make a test pass."""
        assert 20.0 <= VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC <= 200.0

    def test_the_rate_is_not_optimistic_against_the_measurement(self) -> None:
        assert VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC <= MEASURED_CHUNKS_PER_SEC

    def test_the_rate_is_reachable_from_settings_not_only_from_a_constant(self) -> None:
        """#303's docstring claimed "overriding it must not require a code edit".
        As shipped there was no Settings field and both call sites used the
        default, so the claim was false and the keyword was test-only surface."""
        s = Settings(orchestrator_token="x" * 32)
        assert hasattr(s, "validate_assumed_chunks_per_sec")
        assert s.validate_assumed_chunks_per_sec > 0


class TestTheSweepCanMakeProgressAcrossTruncatedRuns:
    def test_candidates_are_ordered_least_recently_attempted_first(self) -> None:
        """STRUCTURAL, and honest about it: this asserts the ordering clause, not
        observed behaviour against a populated DB. The behavioural counterpart
        lives in ``tests/jobs/handlers/test_sweep_ordering.py``.

        ``ORDER BY id`` guarantees a truncated sweep re-does the same head every
        time. Since the sweep is killed at 6h and needs ~147h for the library, the
        tail was unreachable by construction. Oldest-first makes each truncated
        run pick up where the last left off, so the library is covered across runs
        even though no single run can finish.

        RETARGETED on the PR #305 merge (cache validation integrity). This branch
        ordered by ``last_validated_at``; #305 orders by ``last_measure_attempt_at``,
        which is strictly stronger — it is stamped on *every* attempt, success or
        failure, so a game that always times out cannot sit at the head of the
        queue forever the way it could under a column that only moves on success.
        The requirement this test defends is unchanged; only the column is."""
        for sql in (sweep_mod._CANDIDATE_SQL, sweep_mod._CANDIDATE_SQL_FULL):
            assert "ORDER BY id" not in sql, (
                "ordering by id makes a truncated sweep re-validate the same head "
                "forever and never reach the tail"
            )
            assert "last_measure_attempt_at" in sql, (
                "the sweep must order by last_measure_attempt_at so a run that is "
                "cut off still advances coverage"
            )


class TestTheSweepsConcurrencyMatchesWhatTheAgentCanActuallyDo:
    def test_batch_size_does_not_exceed_the_agents_stat_pool(self) -> None:
        """Ten concurrent validates against two stat threads adds queueing, not
        throughput, and silently divides the per-call rate by the batch size — so
        every budget computed from a sequential measurement is wrong under a
        sweep. Pinning these together is what stops them drifting apart again."""
        s = Settings(orchestrator_token="x" * 32)
        assert s.sweep_batch_size <= _CACHE_STAT_WORKERS, (
            f"sweep_batch_size={s.sweep_batch_size} exceeds the agent's "
            f"_CACHE_STAT_WORKERS={_CACHE_STAT_WORKERS}; the extra concurrency "
            f"buys queueing delay and invalidates every per-call budget"
        )
