"""UAT15-B1 (#309): the API must say WHEN a game's cache status was measured.

migration 0015 split cache truth from job outcome, and status_measured_at is the
timestamp that makes the split legible: it records when a measurement actually
looked at the disk. It is populated by record_measurement() and, until this test,
was returned by nothing — not this endpoint, not the CLI (which renders whatever
this endpoint sends), and not Game_shelf.

Karl found it in UAT session 15, scenario 1: "Shows cached. No time on when the
measurement was done." A badge the operator cannot date undercuts the whole point
of separating measurement from job outcome.

last_validated_at is NOT a substitute. It is stamped by attempts too, including
errored ones, so it answers "when did we last try" rather than "when did we last
know".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


class TestStatusMeasuredAtIsExposed:
    async def test_list_rows_carry_status_measured_at(self, client, populated_pool) -> None:
        await populated_pool.execute_write(
            "UPDATE games SET status_measured_at='2026-09-11 21:21:44' WHERE id=1"
        )

        r = await client.get("/api/v1/games", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

        assert r.status_code == 200
        row = next(g for g in r.json()["games"] if g["id"] == 1)
        assert row["status_measured_at"] == "2026-09-11 21:21:44", (
            "the operator cannot judge whether a Cached badge is an hour old or "
            f"three weeks old; got {row.get('status_measured_at')!r}"
        )

    async def test_detail_carries_status_measured_at(self, client, populated_pool) -> None:
        await populated_pool.execute_write(
            "UPDATE games SET status_measured_at='2026-09-11 21:21:44' WHERE id=1"
        )

        r = await client.get("/api/v1/games/1", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

        assert r.status_code == 200
        assert r.json()["game"]["status_measured_at"] == "2026-09-11 21:21:44"

    async def test_a_never_measured_game_reports_null_not_a_guess(
        self, client, populated_pool
    ) -> None:
        """Never measured must be null, never backfilled from last_validated_at.

        An attempt is not a measurement. Substituting one for the other is the
        exact conflation migration 0015 exists to end.
        """
        await populated_pool.execute_write(
            "UPDATE games SET status_measured_at=NULL, "
            "last_validated_at='2026-09-12 05:57:27' WHERE id=1"
        )

        r = await client.get("/api/v1/games/1", headers={"Authorization": f"Bearer {VALID_TOKEN}"})

        assert r.json()["game"]["status_measured_at"] is None
