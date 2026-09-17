"""#339 — a purge must clear SteamPrefill's "already downloaded" record.

Purging a Steam game deleted its chunks and left SteamPrefill's
`successfullyDownloadedDepots.json` untouched. The next cron run therefore saw
"already fetched", skipped the game, and the cache stayed empty. Verified in
production 2026-09-16/17: Alien Shooter purged at 19:24, the cron ran and
completed OK at 12:29 the next day, and issued **zero** depot requests for it.

Removing the depot key by hand made the very next run re-fetch it — 584 depot
requests, 583/583 chunks cached afterwards. That is the mechanism this module
automates.

`handlers/purge.py` claims reversibility because an emptied cache lands in the
set prefill selects on. That holds for Epic, where the orchestrator owns
prefill. It does not hold for Steam — 2540 of 3217 owned games — where prefill
is the host cron driven by this file.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.platform.steam.downloaded_depots import (
    clear_downloaded_depots,
    without_depots,
)

# Shape taken from the live file: {depot_id: [manifest_gids]}, 2375 entries.
LIVE_SHAPE = {
    "33101": [5538627587903293780],
    "1968731": [5597716972109129583, 4380480159779845840],
    "2900140": [5835348148835568030],
}


def test_removes_only_the_named_depot():
    out = without_depots(LIVE_SHAPE, ["33101"])
    assert "33101" not in out
    assert out["1968731"] == [5597716972109129583, 4380480159779845840]
    assert out["2900140"] == [5835348148835568030]


def test_accepts_integer_depot_ids_because_the_file_keys_are_strings():
    """The agent knows depots as ints; the JSON keys them as strings. A type
    mismatch here would silently remove nothing and reinstate the bug."""
    out = without_depots(LIVE_SHAPE, [33101])
    assert "33101" not in out


def test_removing_a_depot_that_is_not_there_is_a_no_op():
    """A purge of a game SteamPrefill never fetched must not fail."""
    out = without_depots(LIVE_SHAPE, ["999999"])
    assert out == LIVE_SHAPE


def test_does_not_mutate_the_caller_state():
    original = json.loads(json.dumps(LIVE_SHAPE))
    without_depots(LIVE_SHAPE, ["33101"])
    assert original == LIVE_SHAPE


def test_no_depots_leaves_the_state_alone():
    assert without_depots(LIVE_SHAPE, []) == LIVE_SHAPE


async def _write(path, data):
    path.write_text(json.dumps(data))


@pytest.mark.asyncio
async def test_clear_rewrites_the_file_without_the_purged_depots(tmp_path):
    p = tmp_path / "successfullyDownloadedDepots.json"
    await _write(p, LIVE_SHAPE)

    removed = await clear_downloaded_depots(p, [33101])

    assert removed == 1
    on_disk = json.loads(p.read_text())
    assert "33101" not in on_disk
    assert len(on_disk) == 2


@pytest.mark.asyncio
async def test_clear_is_a_no_op_when_the_file_does_not_exist(tmp_path):
    """SteamPrefill may never have run on this host. A purge must not fail
    because of a file that was never created."""
    removed = await clear_downloaded_depots(tmp_path / "missing.json", [33101])
    assert removed == 0


@pytest.mark.asyncio
async def test_clear_leaves_a_corrupt_file_untouched(tmp_path):
    """Refuse to guess. Overwriting a file we could not parse would destroy
    SteamPrefill's record of the ENTIRE library to re-fetch one game."""
    p = tmp_path / "successfullyDownloadedDepots.json"
    p.write_text("{not json at all")

    removed = await clear_downloaded_depots(p, [33101])

    assert removed == 0
    assert p.read_text() == "{not json at all", "a corrupt file must be left exactly as found"


@pytest.mark.asyncio
async def test_clear_writes_atomically_so_a_crash_cannot_truncate_it(tmp_path):
    """SteamPrefill reads this file on every run. A half-written file would make
    it re-download the whole library, or fail outright."""
    p = tmp_path / "successfullyDownloadedDepots.json"
    await _write(p, LIVE_SHAPE)

    await clear_downloaded_depots(p, [33101])

    # No temp files left behind, and the result parses.
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != p.name]
    assert leftovers == [], f"atomic write left temp files: {leftovers}"
    json.loads(p.read_text())
