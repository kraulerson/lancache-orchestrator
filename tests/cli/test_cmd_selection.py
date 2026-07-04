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
