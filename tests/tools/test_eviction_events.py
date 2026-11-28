"""Feature B — cache-catcher records each sent eviction alert as an event line.

The orchestrator reads these events (through the agent) and restarts the sweep
pass at the eviction time. Writing them is best-effort: an eviction alert's email
and Kuma push matter more than the event file, so a write failure is logged and
swallowed, never raised into the alert path.
"""

from __future__ import annotations

import json
import re

from tools.cache_catcher.eviction_events import append_event

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def test_append_event_writes_one_valid_json_line(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    logged: list[str] = []
    append_event(str(path), deletes=12, window_s=60, actor="other", log=logged.append)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert set(event) == {"ts", "deletes", "window_s", "actor"}
    assert _TS.match(event["ts"])
    assert event["deletes"] == 12
    assert event["window_s"] == 60
    assert event["actor"] == "other"
    assert logged == []


def test_append_event_appends_rather_than_overwrites(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    append_event(str(path), deletes=10, window_s=60, actor="other", log=lambda _m: None)
    append_event(str(path), deletes=11, window_s=60, actor="nginx", log=lambda _m: None)
    actors = [json.loads(line)["actor"] for line in path.read_text().splitlines()]
    assert actors == ["other", "nginx"]


def test_append_event_never_raises_and_logs_on_failure(tmp_path):
    # A directory where the file should be: open(..., "a") raises IsADirectoryError.
    path = tmp_path / "eviction-events.jsonl"
    path.mkdir()
    logged: list[str] = []
    append_event(str(path), deletes=10, window_s=60, actor="other", log=logged.append)
    assert len(logged) == 1
    assert logged[0].startswith("EVENT-WRITE FAILED (")


def test_append_event_missing_parent_dir_is_logged_not_raised(tmp_path):
    path = tmp_path / "no-such-volume" / "eviction-events.jsonl"
    logged: list[str] = []
    append_event(str(path), deletes=10, window_s=60, actor="other", log=logged.append)
    assert logged and logged[0].startswith("EVENT-WRITE FAILED (")
    assert not path.exists()


def test_mount_status_reports_a_writable_directory(tmp_path):
    from tools.cache_catcher.eviction_events import events_mount_status

    assert events_mount_status(str(tmp_path / "eviction-events.jsonl")) == "writable"


def test_mount_status_reports_a_missing_directory(tmp_path):
    """Final-review I4: logged at guard startup so a missing /events mount is visible."""
    from tools.cache_catcher.eviction_events import events_mount_status

    status = events_mount_status(str(tmp_path / "absent" / "eviction-events.jsonl"))
    assert status.startswith("MISSING")
