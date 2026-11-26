"""Feature B — the guard's alert path writes eviction events correctly (final-review I5).

``fanotify_guard`` loads ``libc.so.6`` at import; patching ``ctypes.CDLL`` with a
MagicMock lets it import off-box (the module only sets argtypes/restype on it at
import time). Its sibling modules are flat imports, as they are inside the
container's /log, so the package directory goes on ``sys.path``.

Pins the spec's "a cooldown-suppressed alert writes no event, and an
orchestrator-actor purge writes no event", plus mode000, and the plan's Review Focus 4:
the email and the Kuma push still happen when the event write fails.
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_PKG = Path(__file__).resolve().parents[2] / "tools" / "cache_catcher"
_MD5 = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_PKG))
    sys.modules.pop("fanotify_guard", None)
    with mock.patch.object(ctypes, "CDLL", return_value=mock.MagicMock()):
        import fanotify_guard as g
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(g, "log", lambda m: calls.append(("log", m)))
    monkeypatch.setattr(g, "send_email", lambda s, b: calls.append(("email", s)))
    monkeypatch.setattr(
        g.kuma, "push", lambda url, status, msg="": calls.append(("kuma", status)) or True
    )
    monkeypatch.setattr(g, "load_cfg", lambda: {"KUMA_PUSH_CACHE_EVICTION": "http://x.invalid"})
    monkeypatch.setattr(g, "EVENTS_PATH", str(tmp_path / "eviction-events.jsonl"))
    g._last_sent.clear()
    g._evict_ts.clear()
    g._purge_ts.clear()
    g.calls = calls
    yield g
    sys.modules.pop("fanotify_guard", None)


def _events(g) -> list[dict]:
    p = Path(g.EVENTS_PATH)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_sent_eviction_alert_writes_one_event_after_email_and_kuma(guard):
    guard.alert("eviction", "subj", "body", deletes=12, actor="nginx")
    events = _events(guard)
    assert len(events) == 1
    assert events[0]["deletes"] == 12
    assert events[0]["actor"] == "nginx"
    kinds = [k for k, _ in guard.calls if k in ("email", "kuma")]
    assert kinds == ["email", "kuma"], "the event must come after both deliveries"


def test_cooldown_suppressed_eviction_writes_no_event(guard):
    guard.alert("eviction", "first", "body", deletes=10, actor="other")
    guard.alert("eviction", "second", "body", deletes=11, actor="other")
    assert len(_events(guard)) == 1


def test_mode000_alert_writes_no_event(guard):
    guard.alert("mode000", "subj", "body")
    assert _events(guard) == []


def test_purge_notice_writes_no_event(guard):
    guard.alert("purge", "subj", "body")
    assert _events(guard) == []


def test_unwritable_events_path_still_sends_email_and_kuma(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "EVENTS_PATH", str(tmp_path / "absent" / "ev.jsonl"))
    guard.alert("eviction", "subj", "body", deletes=10, actor="other")
    kinds = [k for k, _ in guard.calls]
    assert "email" in kinds
    assert "kuma" in kinds
    assert any(m.startswith("EVENT-WRITE FAILED") for k, m in guard.calls if k == "log")


def test_orchestrator_purge_deletes_write_no_event(guard):
    for _ in range(12):
        guard.handle_delete(
            "DELETE", 1, "python", "/app/.venv/bin/python", _MD5, "python -m orchestrator.agent"
        )
    assert _events(guard) == []


def test_uncommanded_deletes_write_an_event_with_the_count(guard):
    for _ in range(10):
        guard.handle_delete("DELETE", 1, "rm", "/usr/bin/rm", _MD5, "rm -rf /volume1/cache")
    events = _events(guard)
    assert len(events) == 1
    assert events[0]["deletes"] == 10
    assert events[0]["actor"] == "other"
