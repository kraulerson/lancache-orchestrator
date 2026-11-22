"""``cache check-new`` — trigger the New - Not Cached check (Feature A)."""

from __future__ import annotations

from click.testing import CliRunner

from orchestrator.cli.main import cli


def _patch(monkeypatch, response, posted):
    class FakeClient:
        def post(self, path, json=None):
            posted["path"] = path
            posted["json"] = json
            return response

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())


def test_check_new_posts_and_reports_queued_or_inflight(monkeypatch):
    posted: dict = {}
    _patch(monkeypatch, {"job_id": 42, "queued": True}, posted)
    result = CliRunner().invoke(cli, ["cache", "check-new"])
    assert result.exit_code == 0
    assert posted["path"] == "/api/v1/new-games-check"
    assert "42" in result.output
    assert "queued" in result.output

    _patch(monkeypatch, {"job_id": 7, "queued": False}, posted)
    result = CliRunner().invoke(cli, ["cache", "check-new"])
    assert result.exit_code == 0
    assert "7" in result.output
    assert "already in flight" in result.output


def test_check_new_help_does_not_overclaim():
    """A manual run neither re-measures overdue games nor pushes the monitors (I2)."""
    result = CliRunner().invoke(cli, ["cache", "check-new", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split()).lower()
    assert "raises the overdue alert" not in text
    assert "re-measures recently added and overdue" not in text
    assert "never pushes either kuma monitor" in text
