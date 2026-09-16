"""#332 — a malformed agent purge response must not strand a stale false green.

`purge_handler` coerced the agent's counts with `int()` BEFORE running the
post-delete measurement. `agent_client` casts `resp.json()` straight to
`dict[str, Any]` with no validation, so a response carrying None or a
non-numeric string raised TypeError/ValueError *after* the files were unlinked
and *before* anything measured the disk.

The result was #310's exact failure mode, reached through a different door: the
files gone, `games.status` still reading its stale pre-purge value, and neither
a validation_history row nor a measurement_transitions row recording that a
purge was ever attempted. Indistinguishable from "no purge happened" until the
next sweep reaches that game — up to a full pass away (~16.4h observed).

The counts are reporting metadata. The measurement is cache truth. A metadata
parse failure must never suppress the truth write.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.purge import purge_handler
from orchestrator.jobs.worker import Deps
from tests.jobs.test_purge_measures_the_disk import (
    _agent_validate,
    _job,
    _latest_validation,
    _seed_game,
    _seed_validation,
    _status,
    _StubAgent,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "bad_value",
    [None, "not-a-number", "", [], {}],
    ids=["none", "string", "empty-string", "list", "dict"],
)
async def test_a_malformed_delete_count_still_measures_the_disk(pool, bad_value):
    """The cache was really emptied; the badge must not stay green because a
    reporting field was the wrong type."""
    game_id = await _seed_game(pool, status="up_to_date")
    await _seed_validation(pool, game_id, total=100, cached=100)

    agent = _StubAgent(
        purge={"deleted": bad_value, "failed": 0, "bytes_freed": 0},
        validate=_agent_validate(total=100, cached=0, outcome="missing"),
    )

    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert agent.validate_calls == 1, "the post-delete measurement must still run"
    assert await _status(pool, game_id) == "not_downloaded", (
        "an emptied cache must be recorded, not left at its stale pre-purge value"
    )
    row = await _latest_validation(pool, game_id)
    assert row is not None and row["chunks_cached"] == 0


async def test_a_malformed_response_leaves_a_record_that_the_purge_happened(pool):
    """The #310 lesson: the danger is not a failed job, it is a database that
    looks exactly like no purge was attempted."""
    game_id = await _seed_game(pool, status="up_to_date")
    await _seed_validation(pool, game_id, total=50, cached=50)

    agent = _StubAgent(
        purge={"deleted": None, "failed": None, "bytes_freed": None},
        validate=_agent_validate(total=50, cached=0, outcome="missing"),
    )

    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    transitions = await pool.read_all(
        "SELECT prior, new_status, commanded FROM measurement_transitions WHERE game_id=?",
        (game_id,),
    )
    assert transitions, "a purge that really deleted must leave a transition row"
    assert transitions[-1]["commanded"] == 1


async def test_a_missing_count_key_is_treated_as_zero_not_an_error(pool):
    """An agent that omits a field entirely is the same class of event as one
    that sends the wrong type. Neither may suppress the measurement."""
    game_id = await _seed_game(pool, status="up_to_date")
    await _seed_validation(pool, game_id, total=10, cached=10)

    agent = _StubAgent(
        purge={},  # no keys at all
        validate=_agent_validate(total=10, cached=10, outcome="cached"),
    )

    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert agent.validate_calls == 1
    assert await _status(pool, game_id) == "up_to_date", (
        "nothing was deleted and the disk still holds it — that is the truth"
    )


async def test_valid_counts_are_still_honoured(pool):
    """Regression guard: defensive parsing must not blunt the real values, which
    still decide the fallback when a post-purge validation errors."""
    game_id = await _seed_game(pool, status="up_to_date")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubAgent(
        purge={"deleted": 300, "failed": 37, "bytes_freed": 123456},
        validate=_agent_validate(total=337, cached=37, outcome="partial"),
    )

    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    row = await _latest_validation(pool, game_id)
    assert row is not None and row["chunks_cached"] == 37
    assert await _status(pool, game_id) == "validation_failed"
