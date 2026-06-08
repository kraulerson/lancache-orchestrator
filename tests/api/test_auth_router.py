"""Tests for POST /api/v1/platforms/steam/auth* (BL10 / F1)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def _clear_challenge_state():
    """Clear module-level _challenges before and after each test.

    Prevents state leakage between tests that store challenge IDs in the
    router's module-level dict.
    """
    from orchestrator.api.routers.auth import _challenges

    _challenges.clear()
    yield
    _challenges.clear()


class TestAuthBegin:
    async def test_happy_path_no_2fa_returns_200(self, client, stub_steam_client):
        # Wire the stub into the auth router's DI
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "no_2fa"
        # Override the dep on whatever app the client was built against:
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "authenticated"
        assert body["steam_id"] == 76561198000000000

    async def test_needs_2fa_returns_202_with_challenge(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "challenge_id" in body
        assert body["challenge_type"] == "mobile_authenticator"
        assert "expires_at" in body

    async def test_bad_credentials_returns_401(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "bad_credentials"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "wrong"},
        )
        assert r.status_code == 401

    async def test_missing_username_returns_400(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"password": "secret"},
        )
        assert r.status_code == 400

    async def test_validation_error_does_not_reflect_submitted_password(
        self, client, stub_steam_client
    ):
        """A body validation error (missing username) must NOT echo the
        submitted password back in the 400 detail. FastAPI's default
        RequestValidationError payload includes `input` (the raw body), which
        would reflect the credential to any client/log capturing the response."""
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        secret = "REFLECT_ME_DO_NOT_aa"  # noqa: S105 test sentinel
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"password": secret},  # username missing -> validation error
        )
        assert r.status_code == 400
        assert secret not in r.text

    async def test_unauth_returns_401(self, client):
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 401

    async def test_non_loopback_returns_403(self, external_client):
        r = await external_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 403

    async def test_no_password_in_logs(self, client, stub_steam_client, capsys):
        from orchestrator.api.routers.auth import get_steam_client_dep
        from orchestrator.core.logging import configure_logging

        configure_logging()
        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        secret = "PASSWORD_DO_NOT_LEAK_aa"  # noqa: S105 test sentinel
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": secret},
        )
        out = capsys.readouterr().out
        assert secret not in out


class TestAuthComplete:
    async def test_good_code_returns_200(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        # First, begin auth so the server stores the challenge
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Now submit the code
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "authenticated"

    async def test_bad_code_returns_401(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Flip scenario for the complete call
        stub_steam_client.scenario = "bad_code"
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "wrong"},
        )
        assert r.status_code == 401

    async def test_unknown_challenge_returns_404(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth/no-such-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "anything"},
        )
        assert r.status_code == 404


class TestAuthStatus:
    async def test_status_returns_authenticated_state(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.get(
            "/api/v1/platforms/steam/auth/status",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["steam_id"] == 76561198000000000

    async def test_status_not_loopback_only(self, external_client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        external_client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: (
            stub_steam_client
        )
        # external_client is not loopback; status should still 200 (not 403)
        r = await external_client.get(
            "/api/v1/platforms/steam/auth/status",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code != 403


class TestPlatformsRowUpdates:
    async def test_successful_auth_updates_platforms_row(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        row = await populated_pool.read_one(
            "SELECT auth_status, last_sync_at, last_error, config FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "ok"
        assert row["last_sync_at"] is not None
        assert row["last_error"] is None
        import json as _json

        config = _json.loads(row["config"])
        assert config["steam_id"] == 76561198000000000
        assert config["username"] == "alice"
        # NEVER persist a token
        assert "password" not in row["config"]
        assert "token" not in row["config"]

    async def test_failed_auth_writes_last_error(self, client, stub_steam_client, populated_pool):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "bad_credentials"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "wrong"},
        )
        row = await populated_pool.read_one(
            "SELECT auth_status, last_error FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "error"
        assert row["last_error"] is not None
        assert "InvalidCredentials" in row["last_error"]


# Issue #95 item 1: 503-path tests for worker-unavailable scenarios
# Issue #95 item 6: external-client 403 for auth_complete loopback enforcement


class TestAuthWorkerUnavailable503Paths:
    """auth_begin + auth_complete should both return 503 when the steam
    worker raises IPCTimeoutError / WorkerDiedError / WorkerDisabledError.
    """

    async def _wire_stub(self, client, stub):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub

    @pytest.mark.parametrize("scenario", ["ipc_timeout", "worker_died", "worker_disabled"])
    async def test_auth_begin_returns_503_on_worker_failure(
        self, client, stub_steam_client, scenario
    ):
        stub_steam_client.scenario = scenario
        await self._wire_stub(client, stub_steam_client)
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "steam worker unavailable"}

    @pytest.mark.parametrize("scenario", ["ipc_timeout", "worker_died", "worker_disabled"])
    async def test_auth_complete_returns_503_on_worker_failure(
        self, client, stub_steam_client, scenario
    ):
        # First create a challenge so auth_complete has something to look up
        stub_steam_client.scenario = "needs_2fa"
        await self._wire_stub(client, stub_steam_client)
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Now flip the scenario for the complete call
        stub_steam_client.scenario = scenario
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "steam worker unavailable"}


class TestAuthCompleteLoopbackEnforcement:
    """Issue #95 item 6: HTTP-level external-client 403 test for
    auth_complete — `TestBL10AuthLoopbackPatterns` validates the regex
    in isolation; this validates the full middleware integration."""

    async def test_external_client_to_auth_complete_returns_403(
        self, external_client, stub_steam_client
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        external_client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: (
            stub_steam_client
        )
        # Use an arbitrary challenge_id — the middleware enforces 403
        # BEFORE the router even sees the request, so the challenge_id
        # doesn't need to exist.
        r = await external_client.post(
            "/api/v1/platforms/steam/auth/any-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 403


class TestAuthChallengeUsernamePreservation:
    """Issue #94: auth_complete must preserve username from in-memory
    challenge state, NOT read it back from platforms.config (which fails
    silently to empty-string on first-ever auth)."""

    async def test_first_ever_2fa_writes_correct_username(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        # Reset platforms.config to NULL to simulate a first-ever auth
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE platforms SET config=NULL WHERE name='steam'")

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "first_ever_user", "password": "secret"},
        )
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 200
        row = await populated_pool.read_one("SELECT config FROM platforms WHERE name='steam'")
        import json as _json

        config = _json.loads(row["config"])
        # Before #94 fix: username would be "" because the DB lookup
        # found NULL config and silently fell back to empty.
        assert config["username"] == "first_ever_user"
        assert config["steam_id"] == 76561198000000000


class TestChallengeSweep:
    """Issue #95 item 3: auth_begin should evict expired _challenges
    entries to prevent unbounded memory growth from abandoned 2FA flows."""

    async def test_expired_challenges_evicted_on_next_auth_begin(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import _challenges, get_steam_client_dep

        # Seed an expired challenge directly into the module state
        _challenges["stale-challenge"] = {
            "expires_at_mono": 0.0,  # epoch — definitely expired
            "username": "ghost_user",
        }
        assert "stale-challenge" in _challenges

        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        # Trigger a successful (no-2FA) auth — the sweep should run even
        # though this code path doesn't store a new challenge.
        # ...wait, no-2FA path returns before reaching the sweep call.
        # Use needs_2fa instead so the sweep IS reached:
        stub_steam_client.scenario = "needs_2fa"
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "fresh", "password": "secret"},
        )
        # The stale entry should be gone; the fresh one should be present
        assert "stale-challenge" not in _challenges
        assert "stub-challenge-id" in _challenges


class TestAuthCompletedTwoFactorLogEvent:
    """Issue #95 item 5: auth_complete success path should emit
    `platform.auth.completed_2fa` (parallel to auth_begin's
    `platform.auth.completed` on the no-2FA path)."""

    async def test_successful_2fa_emits_completed_log_event(
        self, client, stub_steam_client, capsys
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep
        from orchestrator.core.logging import configure_logging

        configure_logging()
        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Drain pre-complete logs
        capsys.readouterr()
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 200
        out = capsys.readouterr().out
        import json as _json

        events = [_json.loads(line) for line in out.splitlines() if line.strip()]
        ev_names = [e.get("event") for e in events]
        assert "platform.auth.completed_2fa" in ev_names


class TestAutoQueueLibrarySync:
    """BL11: both auth-success paths queue a `library_sync` job."""

    async def test_auth_success_no_2fa_queues_library_sync(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        # populated_pool seeds a 'succeeded' library_sync job (id=2). We're
        # asserting on the QUEUED state only; the dedup check ignores
        # terminal states.
        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client

        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 200

        rows = await populated_pool.read_all(
            "SELECT kind, platform, state, source FROM jobs "
            "WHERE kind='library_sync' AND state='queued'"
        )
        assert len(rows) == 1
        assert rows[0] == {
            "kind": "library_sync",
            "platform": "steam",
            "state": "queued",
            "source": "api",
        }

    async def test_auth_complete_2fa_queues_library_sync(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client

        begin = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert begin.status_code == 202
        challenge_id = begin.json()["challenge_id"]

        r = await client.post(
            f"/api/v1/platforms/steam/auth/{challenge_id}",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 200

        rows = await populated_pool.read_all(
            "SELECT kind FROM jobs WHERE kind='library_sync' AND state='queued'"
        )
        assert len(rows) == 1

    async def test_auth_success_skips_queue_when_in_flight_job_exists(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        # Seed an existing queued library_sync job.
        await populated_pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
            ("library_sync", "steam"),
        )
        before = await populated_pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND state='queued'"
        )
        assert len(before) == 1

        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 200

        after = await populated_pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND state='queued'"
        )
        assert len(after) == 1  # no second queued row created

    async def test_production_queue_failure_swallowed_internally(
        self, client, stub_steam_client, unit_app
    ):
        """The real contract: when the pool itself fails on the queue
        INSERT, `_queue_library_sync_job_best_effort` catches PoolError,
        logs warning, returns silently — auth still 200."""
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.api.routers.auth import get_steam_client_dep
        from orchestrator.db.pool import PoolError

        write_count = [0]

        class _PartiallyBrokenPool:
            """Pool that succeeds on the platforms UPDATE but fails on
            the jobs INSERT — simulates a partial outage where the
            primary write is durable but the auto-trigger write fails.
            """

            async def execute_write(self, sql, params=()):
                write_count[0] += 1
                if "INSERT INTO jobs" in sql:
                    raise PoolError("simulated outage on jobs insert")
                # Pretend the platforms UPDATE succeeds.
                return 1

            async def read_one(self, sql, params=()):
                # Dedup-check query returns "no existing job".
                return None

            async def read_all(self, sql, params=()):
                return []

        unit_app.dependency_overrides[get_pool_dep] = lambda: _PartiallyBrokenPool()
        stub_steam_client.scenario = "no_2fa"
        unit_app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client

        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 200  # auth succeeded despite queue failure
        assert write_count[0] >= 1  # the platforms UPDATE did run
