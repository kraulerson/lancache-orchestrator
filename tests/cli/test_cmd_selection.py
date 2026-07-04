"""#229 — ``selection classify`` prefill-exclusion review command."""

from __future__ import annotations

from click.testing import CliRunner

from orchestrator.cli.main import cli


def test_selection_classify_lists_candidates(monkeypatch):
    seen = {}

    class FakeClient:
        def get(self, path, **params):
            seen["path"] = path
            return {
                "candidates": [
                    {
                        "app_id": "220700",
                        "name": "RPG Maker VX Ace",
                        "app_type": "application",
                        "reason": "type=application",
                    },
                    {
                        "app_id": "90",
                        "name": "Half-Life Dedicated Server",
                        "app_type": "game",
                        "reason": "name~'dedicated server'",
                    },
                ],
                "total_candidates": 2,
                "total_scanned": 500,
            }

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "classify"])
    assert result.exit_code == 0
    assert seen["path"] == "/api/v1/selection/candidates"
    assert "220700" in result.output
    assert "RPG Maker VX Ace" in result.output
    assert "dedicated server" in result.output
    assert "2 exclusion candidate" in result.output
    assert "Nothing was changed" in result.output


def test_selection_classify_none(monkeypatch):
    class FakeClient:
        def get(self, path, **params):
            return {"candidates": [], "total_candidates": 0, "total_scanned": 42}

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "classify"])
    assert result.exit_code == 0
    assert "No exclusion candidates" in result.output


def test_selection_allow_posts(monkeypatch):
    seen = {}

    class FakeClient:
        def post(self, path, json=None):
            seen["path"] = path
            seen["json"] = json
            return {}

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "allow", "steam/440"])
    assert result.exit_code == 0
    assert seen["path"] == "/api/v1/prefill-exclusions/steam/440"
    assert seen["json"] == {"mode": "allow"}
    assert "ALLOW" in result.output


def test_selection_exclude_posts(monkeypatch):
    seen = {}

    class FakeClient:
        def post(self, path, json=None):
            seen["json"] = json
            return {}

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "exclude", "steam/1"])
    assert result.exit_code == 0
    assert seen["json"] == {"mode": "exclude"}


def test_selection_unset_deletes(monkeypatch):
    seen = {}

    class FakeClient:
        def delete(self, path, json=None):
            seen["path"] = path
            return {"deleted": 1}

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "unset", "steam/1"])
    assert result.exit_code == 0
    assert seen["path"] == "/api/v1/prefill-exclusions/steam/1"


def test_selection_bad_spec_errors(monkeypatch):
    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: object())
    result = CliRunner().invoke(cli, ["selection", "allow", "no-slash"])
    assert result.exit_code != 0


def test_selection_exclusions_lists(monkeypatch):
    class FakeClient:
        def get(self, path, **params):
            return {
                "exclusions": [
                    {
                        "platform": "steam",
                        "app_id": "1",
                        "mode": "exclude",
                        "source": "classifier",
                        "reason": "auto-classify: type=music",
                    }
                ],
                "total": 1,
            }

    monkeypatch.setattr("orchestrator.cli.commands.selection.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["selection", "exclusions"])
    assert result.exit_code == 0
    assert "type=music" in result.output
