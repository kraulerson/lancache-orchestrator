"""Every declaration of "what a job kind is" must match the database.

UAT-14 (#296). There are three: the ``jobs.kind`` CHECK constraint, the API's
``JobResponse.kind`` Literal, and the CLI's ``--kind`` choices. The CLI had drifted —
it still carried the six kinds from migration 0002 while the constraint had grown
``fetch_manifests`` (0009) and ``purge`` (0014), so ``orchestrator-cli jobs --kind
purge`` was rejected and F18's "audit the purge job" criterion was only half
delivered.

The existing guard could not catch that. ``test_job_response_accepts_all_db_job_kinds``
RESTATES the kind list in its own body, so it went stale in exactly the same way as
the code it guards, and it only asserts acceptance — widening the Literal to ``str``
leaves it green.

These read the constraint out of the migrated schema instead. A list that is derived
cannot disagree with its source.

Lives in tests/db/ for the ``pool`` fixture: ADR-0001 (DQ3) forbids synchronous
``sqlite3``, so the schema is read through the project's own async pool like any
other database access.
"""

from __future__ import annotations

import re
import typing

import pytest

from orchestrator.api.routers.jobs import JobResponse
from orchestrator.cli.commands.jobs import _KINDS as CLI_KINDS

pytestmark = pytest.mark.asyncio

_CONSTRAINT = re.compile(r"kind\s+TEXT\s+NOT NULL\s+CHECK\s*\(\s*kind\s+IN\s*\((.*?)\)\)", re.S)


async def _db_job_kinds(pool) -> frozenset[str]:
    """The kinds the database itself will accept, read from the applied schema."""
    row = await pool.read_one("SELECT sql FROM sqlite_master WHERE name = 'jobs'")
    assert row is not None, "no jobs table in the migrated schema"

    match = _CONSTRAINT.search(row["sql"])
    assert match is not None, f"could not find the kind CHECK constraint in:\n{row['sql']}"

    kinds = frozenset(re.findall(r"'([^']+)'", match.group(1)))
    # Sanity-check the parse itself: if this ever comes back tiny the regex has
    # broken, and every assertion below would pass vacuously.
    assert len(kinds) >= 6, f"parsed implausibly few kinds: {sorted(kinds)}"
    return kinds


async def test_api_model_accepts_exactly_the_db_kinds(pool) -> None:
    db_kinds = await _db_job_kinds(pool)
    literal = frozenset(typing.get_args(JobResponse.model_fields["kind"].annotation))

    assert literal == db_kinds, (
        "JobResponse.kind has drifted from the jobs.kind CHECK constraint.\n"
        f"  only in the model: {sorted(literal - db_kinds)}\n"
        f"  only in the database: {sorted(db_kinds - literal)}\n"
        "A kind the database stores but the model rejects is silently hidden from the API."
    )


async def test_cli_filter_offers_exactly_the_db_kinds(pool) -> None:
    db_kinds = await _db_job_kinds(pool)

    assert frozenset(CLI_KINDS) == db_kinds, (
        "orchestrator-cli jobs --kind has drifted from the jobs.kind CHECK constraint.\n"
        f"  only in the CLI: {sorted(frozenset(CLI_KINDS) - db_kinds)}\n"
        f"  only in the database: {sorted(db_kinds - frozenset(CLI_KINDS))}\n"
        "A kind the database stores but the CLI will not accept cannot be audited."
    )


async def test_the_api_model_is_still_a_closed_set() -> None:
    """Widening the Literal to ``str`` would satisfy an acceptance-only guard.

    The comparison above would fail on a widening too, but only incidentally. State
    it directly: a closed set is what makes an unknown kind a loud error rather than
    a silent pass-through.
    """
    args = typing.get_args(JobResponse.model_fields["kind"].annotation)

    assert args, "JobResponse.kind is no longer a Literal — an unknown kind would pass silently"
    assert all(isinstance(a, str) for a in args)
