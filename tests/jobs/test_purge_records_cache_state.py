"""A purge must record that the cache changed, not just that the game needs re-prefill.

UAT-14 #293, found by the human tester, who declined to tick a clean pass and noted
the chunk counts still read fully cached after a purge:

    status             ⚠ VALIDATION_FAILED
    chunks_cached      337
    chunks_total       337

``chunks_cached`` is not a column on ``games`` — ``api/routers/games.py`` reads it
from the newest ``validation_history`` row. That row is historically accurate; it
really was 337/337 when it was written. The defect is that purge deletes the files
and records nothing, so the newest observation stays the pre-purge one and every
consumer keeps reporting a fully cached game with no files behind it. Game_shelf's
cache badge is driven by exactly those two fields.

So the fix is not to blank a value, it is to record the observation the purge is in
the best possible position to make: it knows precisely what it deleted.
"""

from __future__ import annotations

import pytest

from orchestrator.clients.agent_client import AgentError
from orchestrator.jobs.handlers.purge import purge_handler
from orchestrator.jobs.measurement import record_measurement
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubPurgeAgent:
    """Since #310 the purge measures the disk afterwards, so the stub answers the
    validate call too. ``validate`` defaults to a fully-emptied cache, which is
    what these #293 tests are about; the cases where the measurement disagrees
    with the delete report live in tests/jobs/test_purge_measures_the_disk.py."""

    def __init__(self, *, steam=None, epic=None, raise_exc=None, validate=None):
        self._steam = steam
        self._epic = epic
        self._raise = raise_exc
        self._validate = validate

    async def steam_purge(self, app_id: int):
        if self._raise is not None:
            raise self._raise
        return self._steam

    async def epic_purge(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str):
        if self._raise is not None:
            raise self._raise
        return self._epic

    def _measured(self, game_total: int):
        if self._validate is not None:
            return self._validate
        return {
            "chunks_total": game_total,
            "chunks_cached": 0,
            "chunks_missing": game_total,
            "outcome": "missing",
            "versions": "v1",
        }

    async def steam_validate(self, app_id: int, chunk_count=None):
        return self._measured(chunk_count or 0)

    async def epic_validate(self, **kwargs):
        return self._measured(self._epic.get("deleted", 0) if self._epic else 0)


def _job(game_id: int, platform: str = "steam") -> dict:
    return {"id": 1, "kind": "purge", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, platform="steam", app_id="730", status="up_to_date") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, 't', 1, ?)",
        (platform, app_id, status),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def _seed_validation(pool, game_id: int, *, total: int, cached: int) -> None:
    """The state a healthy validated game is in before anyone purges it."""
    await pool.execute_write(
        "INSERT INTO validation_history (game_id, manifest_version, started_at, finished_at,"
        " method, chunks_total, chunks_cached, chunks_missing, outcome, error) "
        "VALUES (?, 'v1', '2026-08-01T00:00:00Z', '2026-08-01T00:01:00Z', 'disk_stat',"
        " ?, ?, ?, 'cached', NULL)",
        (game_id, total, cached, total - cached),
    )


async def _latest_validation(pool, game_id: int):
    return await pool.read_one(
        "SELECT chunks_total, chunks_cached, chunks_missing, outcome, method "
        "FROM validation_history WHERE game_id = ? ORDER BY id DESC LIMIT 1",
        (game_id,),
    )


async def test_purge_records_that_nothing_is_cached_any_more(pool):
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0, (
        "after a purge the newest observation must say nothing is cached. It still reads "
        f"{latest['chunks_cached']}, so the API and the Game_shelf badge keep reporting a "
        "fully cached game whose files were just deleted."
    )
    assert latest["chunks_total"] == 337, "the manifest size is unchanged by a purge"
    assert latest["chunks_missing"] == 337
    assert latest["outcome"] == "missing"


async def test_purge_does_not_rewrite_the_previous_observation(pool):
    """History is append-only: the pre-purge row was true when it was written."""
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    rows = await pool.read_all(
        "SELECT chunks_cached FROM validation_history WHERE game_id = ? ORDER BY id", (game_id,)
    )
    assert [r["chunks_cached"] for r in rows] == [337, 0], (
        "the original observation must be preserved and a new one appended"
    )


async def test_epic_purge_records_it_too(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="12345")
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, 'v1', ?, 0, 0, 'https://cdn.epicgames.com')",
        (game_id, b"manifest"),
    )
    await _seed_validation(pool, game_id, total=50, cached=50)

    agent = _StubPurgeAgent(epic={"deleted": 50, "failed": 0, "bytes_freed": 42})
    await purge_handler(_job(game_id, platform="epic"), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0
    assert latest["outcome"] == "missing"


async def test_a_failed_purge_records_nothing(pool):
    """If the agent never deleted anything, no observation was made."""
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(raise_exc=AgentError("agent unreachable"))
    with pytest.raises(AgentError):
        await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 337, (
        "a purge that failed must not claim the cache is empty — that would be the same "
        "false report in the opposite direction"
    )


async def test_purge_with_no_prior_validation_measures_instead_of_inventing(pool):
    """Inventing a total would be a lie — but #310 no longer has to guess one.

    Before #310 the handler carried chunks_total forward from the previous
    validation, so a game that had never been validated had no size to carry and
    the only honest move was to write nothing. The purge now runs a real
    validation, which enumerates the manifest itself, so the observation is
    measured rather than fabricated and the row is legitimate.
    """
    game_id = await _seed_game(pool, app_id="440")

    agent = _StubPurgeAgent(
        steam={"deleted": 0, "failed": 0, "bytes_freed": 0},
        validate={
            "chunks_total": 12,
            "chunks_cached": 0,
            "chunks_missing": 12,
            "outcome": "missing",
            "versions": "v1",
        },
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_total"] == 12, "the size came from the measurement, not from thin air"
    assert latest["chunks_cached"] == 0

    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "not_downloaded", "still in the set F5/F6 re-prefill from"


async def test_the_status_and_the_observation_land_together(pool):
    """Flagging for re-prefill and recording the cache state are one atomic write.

    They were two separate execute_write calls. A crash or PoolError between them
    left the files deleted, the status flipped, and the newest validation_history
    row still reading 337/337 cached — exactly the badge #293 exists to fix,
    resurrected for up to 6h until the sweep revalidates.

    Since #310 that pairing lives in ``validate_one_game``, which purge now shares
    with the validate job and the sweep, so this asserts it there: fail the
    measurement and the observation written beside it must roll back with it.
    """
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})

    import orchestrator.jobs.handlers.validate as validate_mod

    original = validate_mod.record_measurement

    async def boom(*a, **kw):
        raise RuntimeError("write failed between the two statements")

    validate_mod.record_measurement = boom
    try:
        with pytest.raises(RuntimeError):
            await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))
    finally:
        validate_mod.record_measurement = original

    g = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date", (
        "the status flip must roll back with the failed observation — otherwise the "
        "game reads not_downloaded while the newest row still claims 337/337"
    )
    rows = await pool.read_all(
        "SELECT chunks_cached FROM validation_history WHERE game_id = ? ORDER BY id", (game_id,)
    )
    assert [r["chunks_cached"] for r in rows] == [337], (
        "the new observation must have rolled back too, leaving only the pre-purge row"
    )


async def _prime_breaker(pool, n: int = 24) -> None:
    """Leave the measurement circuit breaker one transition short of tripping.

    Not a synthetic seed: these are real downward measurements, exactly what a
    lancache eviction event produces — the incident this feature exists to
    detect. The default 24 sits one below the default threshold of 25.
    """
    for i in range(n):
        gid = await _seed_game(pool, app_id=f"prime{i}", status="up_to_date")
        await record_measurement(pool, gid, "missing")


async def test_a_purge_is_never_refused_by_the_circuit_breaker(pool):
    """Security audit SEV-2. The files are already gone when the record is written.

    The breaker counts downward transitions globally, so 24 evictions plus one
    operator purge reached the threshold: record_measurement raised, the purge's
    transaction rolled back, and the game kept status='up_to_date' with its
    pre-purge 337/337 history row on top — a green "Cached 337/337" badge over an
    empty cache. Worse, the correction path was disabled by the same breaker (the
    next sweep's downward write is refused too) and the row stayed ineligible for
    re-prefill because its status never moved.

    A purge is a COMMANDED change with an authoritative cause. The breaker exists
    to veto an agent that may be lying about what it read; it must not veto a
    record of files this system itself deleted.
    """
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)
    await _prime_breaker(pool)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    g = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (game_id,))
    assert g["status"] == "not_downloaded", (
        "the purge deleted the files; refusing to record that leaves a green badge "
        "over an empty cache that no later sweep can correct. (The status is the "
        "measured one since #310 — 'missing' on an emptied cache — where this used "
        "to be a hard-coded 'partial'.)"
    )
    assert g["status_measured_at"] is not None
    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0
    assert latest["outcome"] == "missing"
    # A command is not an observation: it is not vetoed by the mass-loss alarm and
    # does not feed it. Since #310 it IS logged — a bulk purge leaving no trace in
    # the record of cache truth was the wrong way to buy that immunity — and the
    # mark is what keeps it out of the breaker's count.
    rows = await pool.read_all(
        "SELECT downward, commanded FROM measurement_transitions WHERE game_id=?", (game_id,)
    )
    assert len(rows) == 1
    assert rows[0]["commanded"] == 1
