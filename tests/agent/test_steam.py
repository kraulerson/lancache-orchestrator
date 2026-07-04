"""Tests for the agent /v1/steam/* endpoints."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.platform.steam.prefill_driver import PrefillResult, SteamAuthStatus


class _FakeDriver:
    def __init__(self):
        self.calls = []

    async def prefill_apps(self, app_ids, *, force=False):
        self.calls.append((app_ids, force))
        return PrefillResult(ok=True, raw="OK done")

    def downloaded_state(self):
        return {440: [111, 222]}

    def auth_status(self):
        return SteamAuthStatus(ok=True)


def _client(driver) -> TestClient:
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.prefill_driver = driver
    return TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})


def test_steam_prefill_runs_to_done():
    driver = _FakeDriver()
    client = _client(driver)
    resp = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/steam/prefill/{job_id}").json()
        if snap["state"] == "done":
            break
        time.sleep(0.02)  # let the offloaded capture (asyncio.to_thread) finish
    assert snap["state"] == "done"
    assert snap["result"] == {"ok": True, "raw": "OK done"}
    assert driver.calls == [([440], False)]


def test_prefill_captures_manifest_to_archive(tmp_path):
    """After a successful prefill, the manifest SteamPrefill wrote to its HOME
    cache (steam_prefill_live_cache_dir) is captured into the durable archive —
    so agent-driven force-prefills' manifests get validated against instead of a
    stale archived version (the false-Partial root cause)."""
    live = tmp_path / "live"
    archive = tmp_path / "archive"
    (live / "v1").mkdir(parents=True)

    class _ManifestWritingDriver(_FakeDriver):
        async def prefill_apps(self, app_ids, *, force=False):
            # SteamPrefill writes its manifest to its HOME cache during a prefill.
            (live / "v1" / "440_440_441_777.bin").write_bytes(b"manifest")
            return await super().prefill_apps(app_ids, force=force)

    app = create_agent_app(
        settings=Settings(
            orchestrator_token="a" * 32,
            steam_prefill_live_cache_dir=live,
            steam_manifest_archive_dir=archive,
        )
    )
    app.state.prefill_driver = _ManifestWritingDriver()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})

    job_id = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False}).json()[
        "job_id"
    ]
    for _ in range(50):
        snap = client.get(f"/v1/steam/prefill/{job_id}").json()
        if snap["state"] == "done":
            break
        time.sleep(0.02)  # let the offloaded capture (asyncio.to_thread) finish
    assert snap["state"] == "done"
    assert (archive / "v1" / "440_440_441_777.bin").exists()


def test_prefill_capture_failure_does_not_fail_the_job(tmp_path):
    """A capture failure (e.g. unwritable archive) must never fail the prefill."""

    class _ManifestWritingDriver(_FakeDriver):
        async def prefill_apps(self, app_ids, *, force=False):
            return await super().prefill_apps(app_ids, force=force)

    app = create_agent_app(
        settings=Settings(
            orchestrator_token="a" * 32,
            steam_prefill_live_cache_dir=tmp_path / "missing-live",  # no /v1 -> sync is a no-op
            steam_manifest_archive_dir=tmp_path / "archive",
        )
    )
    app.state.prefill_driver = _ManifestWritingDriver()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    job_id = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False}).json()[
        "job_id"
    ]
    for _ in range(50):
        snap = client.get(f"/v1/steam/prefill/{job_id}").json()
        if snap["state"] == "done":
            break
        time.sleep(0.02)  # let the offloaded capture (asyncio.to_thread) finish
    assert snap["state"] == "done"
    assert snap["result"]["ok"] is True


def test_prefill_warns_when_live_cache_dir_missing(tmp_path):
    """A successful prefill whose live cache /v1 dir is absent (the HOME-drift
    symptom) must log a loud WARNING — otherwise the capture silently no-ops and
    false-Partial badges silently return (UAT-13 F2b / #211)."""
    import structlog

    app = create_agent_app(
        settings=Settings(
            orchestrator_token="a" * 32,
            steam_prefill_live_cache_dir=tmp_path / "missing-live",  # no /v1 subdir
            steam_manifest_archive_dir=tmp_path / "archive",
        )
    )
    app.state.prefill_driver = _FakeDriver()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    with structlog.testing.capture_logs() as logs:
        job_id = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False}).json()[
            "job_id"
        ]
        for _ in range(50):
            snap = client.get(f"/v1/steam/prefill/{job_id}").json()
            if snap["state"] == "done":
                break
            time.sleep(0.02)
    assert snap["state"] == "done"
    warnings = [m for m in logs if m.get("event") == "steam_prefill.live_cache_missing"]
    assert warnings, "expected a live_cache_missing warning when the live cache dir is absent"
    assert warnings[0]["log_level"] == "warning"


def test_prefill_no_warning_when_live_cache_dir_present(tmp_path):
    """The drift warning must NOT fire on the normal path (live cache dir exists,
    even if nothing new was copied) — it is a path-mismatch signal, not a
    nothing-to-capture signal."""
    import structlog

    live = tmp_path / "live"
    (live / "v1").mkdir(parents=True)
    app = create_agent_app(
        settings=Settings(
            orchestrator_token="a" * 32,
            steam_prefill_live_cache_dir=live,
            steam_manifest_archive_dir=tmp_path / "archive",
        )
    )
    app.state.prefill_driver = _FakeDriver()  # writes no manifest -> 0 copied, dir present
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    with structlog.testing.capture_logs() as logs:
        job_id = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False}).json()[
            "job_id"
        ]
        for _ in range(50):
            snap = client.get(f"/v1/steam/prefill/{job_id}").json()
            if snap["state"] == "done":
                break
            time.sleep(0.02)
    assert snap["state"] == "done"
    assert not [m for m in logs if m.get("event") == "steam_prefill.live_cache_missing"]


def test_downloaded_state():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/downloaded-state")
    assert resp.status_code == 200
    assert resp.json() == {"440": [111, 222]}


def test_auth_status():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/auth-status")
    assert resp.json() == {"ok": True, "reason": ""}


def test_steam_prefill_rejects_negative_app_id():
    client = _client(_FakeDriver())
    resp = client.post("/v1/steam/prefill", json={"app_ids": [-5], "force": False})
    assert resp.status_code == 422


class _FakeFetcher:
    def __init__(self, result=None, boom=False):
        from orchestrator.platform.steam.manifest_fetcher import FetchResult

        self._result = result or FetchResult(fetched=2, skipped=1, failed=0, apps=3)
        self._boom = boom

    def fetch_all(self):
        if self._boom:
            raise RuntimeError("fetch blew up")
        return self._result


def test_fetch_manifests_requires_bearer():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher()
    client = TestClient(app)  # no Authorization header
    assert client.post("/v1/steam/fetch-manifests").status_code == 401


def test_fetch_manifests_runs_to_done():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    job_id = client.post("/v1/steam/fetch-manifests").json()["job_id"]
    snap: dict = {}
    for _ in range(50):
        snap = client.get(f"/v1/steam/fetch-manifests/{job_id}").json()
        if snap["state"] == "done":
            break
        time.sleep(0.02)
    assert snap["state"] == "done"
    assert snap["result"] == {"fetched": 2, "skipped": 1, "failed": 0, "apps": 3}


def test_fetch_manifests_records_failure():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher(boom=True)
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    job_id = client.post("/v1/steam/fetch-manifests").json()["job_id"]
    snap: dict = {}
    for _ in range(50):
        snap = client.get(f"/v1/steam/fetch-manifests/{job_id}").json()
        if snap["state"] == "failed":
            break
        time.sleep(0.02)
    assert snap["state"] == "failed"


def test_fetch_manifests_single_flight_reuses_running_job():
    """A second POST /v1/steam/fetch-manifests returns the existing job_id while one is running."""
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})

    # Pre-seed an in-flight running job directly in the store.
    # AgentJobStore.create() initialises state="running" by default.
    store = app.state.agent_jobs
    existing_job_id = store.create()
    app.state.fetch_manifests_job = existing_job_id

    resp = client.post("/v1/steam/fetch-manifests")
    assert resp.status_code == 202
    assert resp.json()["job_id"] == existing_job_id
    # The store should NOT have grown (no new job was created)
    assert store.size() == 1


def test_prefilled_apps_lists_distinct_app_ids(tmp_path):
    v1 = tmp_path / "v1"
    v1.mkdir()
    for name in ("440_440_441_1.bin", "440_440_442_2.bin", "730_730_731_3.bin"):
        (v1 / name).write_bytes(b"")
    app = create_agent_app(
        settings=Settings(orchestrator_token="a" * 32, steam_manifest_cache_dir=tmp_path)
    )
    app.state.prefill_driver = _FakeDriver()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.get("/v1/steam/prefilled-apps")
    assert resp.status_code == 200
    assert resp.json() == {"app_ids": [440, 730]}


def test_prune_selection_removes_and_backs_up(tmp_path):
    import json

    (tmp_path / "selectedAppsToPrefill.json").write_text(json.dumps([1, 2, 3]))
    app = create_agent_app(
        settings=Settings(orchestrator_token="a" * 32, steam_prefill_config_dir=tmp_path)
    )
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post(
        "/v1/steam/prune-selection", json={"exclude_app_ids": [2], "restore_app_ids": []}
    )
    assert resp.status_code == 200
    assert resp.json() == {"removed": 1, "restored": 0, "remaining": 2}
    assert json.loads((tmp_path / "selectedAppsToPrefill.json").read_text()) == [1, 3]
    # the ORIGINAL curated list is preserved in the .bak sidecar
    assert json.loads((tmp_path / "selectedAppsToPrefill.json.bak").read_text()) == [1, 2, 3]


def test_prune_selection_restore_readds(tmp_path):
    import json

    (tmp_path / "selectedAppsToPrefill.json").write_text(json.dumps([1]))
    app = create_agent_app(
        settings=Settings(orchestrator_token="a" * 32, steam_prefill_config_dir=tmp_path)
    )
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post(
        "/v1/steam/prune-selection", json={"exclude_app_ids": [], "restore_app_ids": [5]}
    )
    assert resp.json()["restored"] == 1
    assert json.loads((tmp_path / "selectedAppsToPrefill.json").read_text()) == [1, 5]


def test_prune_selection_noop_no_backup(tmp_path):
    import json

    (tmp_path / "selectedAppsToPrefill.json").write_text(json.dumps([1, 2]))
    app = create_agent_app(
        settings=Settings(orchestrator_token="a" * 32, steam_prefill_config_dir=tmp_path)
    )
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post("/v1/steam/prune-selection", json={"exclude_app_ids": [9]})
    assert resp.json()["removed"] == 0
    assert not (tmp_path / "selectedAppsToPrefill.json.bak").exists()


def test_prune_selection_missing_file(tmp_path):
    app = create_agent_app(
        settings=Settings(orchestrator_token="a" * 32, steam_prefill_config_dir=tmp_path)
    )
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post("/v1/steam/prune-selection", json={"exclude_app_ids": [1]})
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0
