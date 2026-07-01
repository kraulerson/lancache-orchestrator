"""F11 — ``cache`` subcommands (validate-all backfill trigger, 2026-06-24)."""

from __future__ import annotations

from click.testing import CliRunner

from orchestrator.cli.main import cli


def test_cache_validate_all_posts_full_sweep(monkeypatch):
    posted = {}

    class FakeClient:
        def post(self, path, json=None):
            posted["path"] = path
            posted["json"] = json
            return {"job_id": 42, "full": True, "queued": True}

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["cache", "validate-all"])
    assert result.exit_code == 0
    assert posted["path"] == "/api/v1/sweep"
    assert posted["json"] == {"full": True}
    assert "42" in result.output
    assert "queued full validation sweep" in result.output


def test_cache_validate_all_warns_when_not_full(monkeypatch):
    """full=true deduped against an in-flight NON-full sweep: warn, don't mislead."""

    class FakeClient:
        def post(self, path, json=None):
            return {"job_id": 7, "full": False, "queued": False}

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["cache", "validate-all"])
    assert result.exit_code == 0
    assert "7" in result.output
    assert "NOT a full backfill" in result.output
    assert "already in flight" in result.output


def test_cache_fetch_manifests_posts(monkeypatch):
    """cache fetch-manifests POSTs to /api/v1/fetch-manifests and reports job_id."""
    posted = {}

    class FakeClient:
        def post(self, path, json=None):
            posted["path"] = path
            posted["json"] = json
            return {"job_id": 7, "queued": True}

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["cache", "fetch-manifests"])
    assert result.exit_code == 0
    assert posted["path"] == "/api/v1/fetch-manifests"
    assert "7" in result.output


def test_cache_fetch_manifests_warns_when_already_inflight(monkeypatch):
    """Deduped against an in-flight fetch: warn with job_id."""

    class FakeClient:
        def post(self, path, json=None):
            return {"job_id": 9, "queued": False}

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["cache", "fetch-manifests"])
    assert result.exit_code == 0
    assert "9" in result.output
    assert "already in flight" in result.output
