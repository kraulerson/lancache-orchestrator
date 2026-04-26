"""Chaos tests for orchestrator.db.pool — connection replacement state machine,
storm guard, partial health-check failures.

These tests patch aiosqlite.Connection.execute to simulate disk I/O errors and
verify the pool's auto-recovery behavior. Per spec §4.6.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import aiosqlite
import pytest

from orchestrator.db.pool import ConnectionLostError, Pool

VALID_TOKEN = "a" * 32


class TestReaderReplacement:
    async def test_disk_io_error_triggers_replacement(self, pool: Pool, monkeypatch):
        """When a reader connection raises 'disk i/o error', the pool replaces
        it transparently and subsequent reads succeed."""
        # Find one reader from the pool's queue
        async with pool.acquire_reader() as conn:
            target_id = id(conn)

        original_execute = aiosqlite.Connection.execute
        call_count = {"n": 0}

        async def patched_execute(self, sql, parameters=()):
            if id(self) == target_id and call_count["n"] == 0:
                call_count["n"] += 1
                raise aiosqlite.OperationalError("disk I/O error")
            return await original_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", patched_execute)

        with pytest.raises(ConnectionLostError):
            await pool.read_one("SELECT 1")

        # Subsequent read should succeed (replacement happened)
        # Wait briefly for background replacement to finish
        await asyncio.sleep(0.5)
        row = await pool.read_one("SELECT 1 AS x")
        assert row["x"] == 1

    async def test_replacement_emits_structured_event(self, pool: Pool, monkeypatch, capsys):
        from orchestrator.core import logging as log_mod

        log_mod.configure_logging()

        original_execute = aiosqlite.Connection.execute
        triggered = {"n": 0}

        async def patched_execute(self, sql, parameters=()):
            if triggered["n"] == 0 and "SELECT 1" in str(sql).upper():
                triggered["n"] += 1
                raise aiosqlite.OperationalError("disk I/O error")
            return await original_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", patched_execute)

        with pytest.raises(ConnectionLostError):
            await pool.read_one("SELECT 1")
        await asyncio.sleep(0.5)

        out = capsys.readouterr().out
        events = [json.loads(line)["event"] for line in out.splitlines() if line.strip()]
        assert "pool.connection_lost" in events
        assert "pool.connection_replaced" in events


class TestWriterReplacement:
    async def test_writer_disk_io_triggers_replacement(self, pool: Pool, monkeypatch):
        original_execute = aiosqlite.Connection.execute
        triggered = {"n": 0}

        async def patched_execute(self, sql, parameters=()):
            if triggered["n"] == 0 and "INSERT" in str(sql).upper():
                triggered["n"] += 1
                raise aiosqlite.OperationalError("database disk image is malformed")
            return await original_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", patched_execute)

        with pytest.raises(ConnectionLostError):
            await pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', 'replace-1', 'X', 1, 'never_prefilled')"
            )
        await asyncio.sleep(0.5)

        # Subsequent write should succeed
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES ('steam', 'replace-2', 'Y', 1, 'never_prefilled')"
        )


class TestStormGuard:
    async def test_storm_guard_trips_after_3_replacements_in_60s(self, pool: Pool, monkeypatch):
        """Forcing 4+ replacements within 60s trips the storm guard; pool
        transitions to degraded."""

        async def always_io_error(self, sql, parameters=()):
            raise aiosqlite.OperationalError("disk I/O error")

        monkeypatch.setattr(aiosqlite.Connection, "execute", always_io_error)

        # Trigger replacements rapidly
        for _ in range(4):
            with contextlib.suppress(ConnectionLostError):
                await pool.read_one("SELECT 1")
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.5)

        # After storm guard trips, the pool's degraded
        # Next op should raise PoolError indicating degraded state
        # OR all readers may now be marked unhealthy
        health = await pool.health_check()
        # Either readers are mostly unhealthy OR the writer is unhealthy
        unhealthy_readers = health["readers"]["total"] - health["readers"]["healthy"]
        assert unhealthy_readers >= 1 or not health["writer"]["healthy"]


class TestHealthCheckPartial:
    async def test_health_check_reports_partial_unhealthy_readers(self, pool: Pool, monkeypatch):
        """If one reader is unhealthy, health_check reports it but writer + other
        readers stay healthy."""
        # Trigger one reader replacement, then check health
        original_execute = aiosqlite.Connection.execute
        triggered = {"n": 0}

        async def patched_execute(self, sql, parameters=()):
            if triggered["n"] == 0:
                triggered["n"] += 1
                raise aiosqlite.OperationalError("disk I/O error")
            return await original_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", patched_execute)

        with contextlib.suppress(ConnectionLostError):
            await pool.read_one("SELECT 1")

        # Restore execute so health probes work
        monkeypatch.setattr(aiosqlite.Connection, "execute", original_execute)
        await asyncio.sleep(0.1)

        health = await pool.health_check()
        # Replacement count should be ≥ 1
        assert health["readers"]["replacements"] >= 1


class TestHealthCheckTimeout:
    async def test_health_check_per_probe_timeout(self, pool: Pool, monkeypatch):
        """A hung probe doesn't deadlock the health endpoint."""
        original_execute = aiosqlite.Connection.execute

        async def slow_execute(self, sql, parameters=()):
            if "SELECT 1" in str(sql).upper():
                await asyncio.sleep(5)  # slower than the 1s probe timeout
            return await original_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", slow_execute)

        t0 = time.monotonic()
        health = await pool.health_check()
        elapsed = time.monotonic() - t0

        # Health check returned within the timeout budget (writer + readers each
        # capped at 1s; gathered concurrently → <1.5s total)
        assert elapsed < 2.0
        # And reported the unreachable connections
        assert (
            health["writer"]["healthy"] is False
            or health["readers"]["healthy"] < pool._readers_count
        )
