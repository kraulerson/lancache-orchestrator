"""Settings for the New - Not Cached feature (new_games_check job, poller, monitors)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.core.settings import Settings
from tests.core.conftest import VALID_TOKEN


def _make() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


def test_defaults():
    s = _make()
    assert s.new_game_window_days == 7
    assert s.new_game_unknown_alert_hours == 48
    assert s.new_games_check_enabled is True
    assert s.new_games_check_cron == "30 2,8,14,20 * * *"
    assert s.steam_download_poll_sec == 600
    assert s.kuma_push_new_games is None
    assert s.kuma_push_new_games_overdue is None


def test_bad_cron_rejected_at_load(monkeypatch):
    monkeypatch.setenv("ORCH_NEW_GAMES_CHECK_CRON", "bad")
    with pytest.raises(ValidationError):
        _make()


def test_poll_zero_accepted(monkeypatch):
    monkeypatch.setenv("ORCH_STEAM_DOWNLOAD_POLL_SEC", "0")
    assert _make().steam_download_poll_sec == 0


@pytest.mark.parametrize("value", ["1", "30", "59"])
def test_poll_between_one_and_fifty_nine_rejected(monkeypatch, value):
    """M7: a sub-minute poll would hammer the agent; 0 (off) or >=60 only."""
    monkeypatch.setenv("ORCH_STEAM_DOWNLOAD_POLL_SEC", value)
    with pytest.raises(ValidationError):
        _make()


def test_poll_sixty_accepted(monkeypatch):
    monkeypatch.setenv("ORCH_STEAM_DOWNLOAD_POLL_SEC", "60")
    assert _make().steam_download_poll_sec == 60


def test_window_zero_rejected(monkeypatch):
    monkeypatch.setenv("ORCH_NEW_GAME_WINDOW_DAYS", "0")
    with pytest.raises(ValidationError):
        _make()


def test_alert_hours_below_floor_rejected(monkeypatch):
    monkeypatch.setenv("ORCH_NEW_GAME_UNKNOWN_ALERT_HOURS", "5")
    with pytest.raises(ValidationError):
        _make()
