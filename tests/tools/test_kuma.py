"""Kuma push for cache-catcher — stdlib only, and it must never raise.

`fanotify_guard.py` has no third-party dependencies and cannot gain any: the
cache-catcher container has no pip packages. This mirrors the semantics already
settled twice elsewhere in this system — `src/orchestrator/clients/heartbeat.py`
and `push_kuma()` in `run-steam-prefill.sh` — so there is one story for what a
heartbeat means rather than three.

The load-bearing property is that **monitoring must never break the thing it
monitors**. A guard that dies because Kuma was unreachable is worse than no
guard at all.
"""

from __future__ import annotations

import urllib.parse

import pytest
from tools.cache_catcher.kuma import MSG_MAX_CHARS, push


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recorder(status=200, raises=None):
    """Return a fake urlopen plus the list of URLs it was asked for."""
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        if raises is not None:
            raise raises
        return _FakeResponse(status)

    return opener, calls


@pytest.mark.parametrize("url", [None, "", "   "], ids=["none", "empty", "blank"])
def test_an_unset_url_disables_the_push_and_is_not_a_failure(url):
    """Leaving a URL unset is the documented way to turn a monitor off. It must
    not look like an undelivered heartbeat, or a caller that retries on False
    would retry forever against a monitor that was switched off on purpose."""
    opener, calls = _recorder()
    assert push(url, "up", "anything", opener=opener) is True
    assert calls == []


def test_a_delivered_heartbeat_reports_true():
    opener, calls = _recorder(status=200)
    assert push("http://kuma/push/abc", "up", "fine", opener=opener) is True
    assert len(calls) == 1


def test_status_and_message_are_url_encoded_into_the_query():
    """A message containing % or & must not corrupt the request. The LXC disk
    monitor's first version was destroyed by exactly one unescaped percent: it
    installed cleanly, reported no error, and never ran."""
    opener, calls = _recorder()
    push("http://kuma/push/abc", "down", "zone 94% full & climbing", opener=opener)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
    assert query["status"] == ["down"]
    assert query["msg"] == ["zone 94% full & climbing"]


def test_an_http_error_reports_undelivered():
    """A mistyped push token answers 404: the request completed and nobody was
    told. For every caller that is the same outcome as a connection failure."""
    opener, _ = _recorder(status=404)
    assert push("http://kuma/push/typo", "up", opener=opener) is False


def test_a_connection_failure_reports_undelivered_and_never_raises():
    opener, _ = _recorder(raises=OSError("connection refused"))
    assert push("http://kuma/push/abc", "up", opener=opener) is False


def test_a_long_message_keeps_its_tail():
    """For an error the specific failure is at the end, not the start."""
    opener, calls = _recorder()
    push("http://kuma/push/abc", "down", "x" * 500 + "THE-REASON", opener=opener)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
    assert query["msg"][0].endswith("THE-REASON")
    assert len(query["msg"][0]) == MSG_MAX_CHARS
