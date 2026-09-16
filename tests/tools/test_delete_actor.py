"""#337 — cache-catcher must tell a commanded purge from an uncommanded eviction.

On 2026-09-16 the operator got an emergency email reading "cache eviction
detected (583 deletes/60s)". There was no eviction: it was purge job 46589
deleting Alien Shooter's 583 chunks, an operator-initiated action the
orchestrator had already recorded as `commanded = 1` so the circuit breaker
would ignore it.

cache-catcher's own log named the culprit correctly on every line
(`cmd=[/app/.venv/bin/python -m orchestrator.agent]`) and then labelled it an
eviction anyway. The information was in hand and unused.

The cost is not one email. It is that every future purge cries wolf, and the
alert that matters — nginx's cache-manager evicting from a full keys_zone, the
2026-07-31 mass-deletion signature — arrives in an inbox trained to dismiss it.

This module is deliberately dependency-free: `fanotify_guard.py` loads
`libc.so.6` at import, so it cannot be imported on a developer machine. The
decision logic lives here so it can be tested anywhere.
"""

from __future__ import annotations

import pytest
from tools.cache_catcher.delete_actor import (
    ACTOR_NGINX,
    ACTOR_ORCHESTRATOR,
    ACTOR_OTHER,
    classify_actor,
    counts_toward_eviction,
)

# The exact line cache-catcher logged during the false alarm.
REAL_PURGE = {
    "comm": "python",
    "exe": "/usr/local/bin/python3.12",
    "cmd": "/app/.venv/bin/python -m orchestrator.agent",
}


def test_the_real_false_alarm_is_recognised_as_the_orchestrator():
    assert classify_actor(**REAL_PURGE) == ACTOR_ORCHESTRATOR


def test_a_commanded_purge_does_not_count_toward_the_eviction_signature():
    """The whole point. These deletes are deliberate and already recorded as
    commanded in measurement_transitions; they must not inflate the counter that
    triggers the mass-eviction alarm."""
    assert counts_toward_eviction(classify_actor(**REAL_PURGE)) is False


def test_nginx_still_counts_toward_the_eviction_signature():
    """The real signal must be untouched. nginx's cache-manager evicting from a
    nearly-full keys_zone is the 2026-07-31 incident this monitor exists for."""
    actor = classify_actor(comm="nginx", exe="/usr/sbin/nginx", cmd="nginx: cache manager process")
    assert actor == ACTOR_NGINX
    assert counts_toward_eviction(actor) is True


def test_an_unrecognised_deleter_still_counts():
    """Fail loud: something unexpected deleting cache files is exactly what this
    monitor is for. Unknown must never be treated as benign."""
    actor = classify_actor(comm="rm", exe="/bin/rm", cmd="rm -rf /volume1/cache/ab")
    assert actor == ACTOR_OTHER
    assert counts_toward_eviction(actor) is True


def test_nfsd_counts_as_unknown_and_alerts():
    """The original mass-deletion investigation suspected nfsd. It must alert."""
    actor = classify_actor(comm="nfsd", exe="", cmd="")
    assert actor == ACTOR_OTHER
    assert counts_toward_eviction(actor) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "/bin/rm -rf /opt/orchestrator-backup",
        "tar czf orchestrator.tar.gz /volume1/cache",
        "python -m orchestrator_agent_lookalike",
        "vim /log/orchestrator.agent.notes",
    ],
    ids=["backup-path", "tarball-name", "lookalike-module", "filename"],
)
def test_the_word_orchestrator_alone_does_not_excuse_a_deleter(cmd):
    """Precision matters more than recall here. Matching loosely on the word
    'orchestrator' would let a stray `rm` under an orchestrator-named path
    silently suppress a real mass deletion — turning the monitor off in exactly
    the case it exists for."""
    actor = classify_actor(comm="rm", exe="/bin/rm", cmd=cmd)
    assert actor != ACTOR_ORCHESTRATOR
    assert counts_toward_eviction(actor) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"comm": None, "exe": None, "cmd": None},
        {"comm": "", "exe": "", "cmd": ""},
    ],
    ids=["none", "empty"],
)
def test_missing_process_details_never_crash_and_never_excuse(kwargs):
    """fanotify can lose /proc details when the process exits before the event is
    read. A delete we cannot attribute must still alert."""
    actor = classify_actor(**kwargs)
    assert actor == ACTOR_OTHER
    assert counts_toward_eviction(actor) is True
