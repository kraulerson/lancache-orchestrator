"""Uptime Kuma push heartbeats for cache-catcher.

Wire format is a GET: ``<url>?status=up|down&msg=<text>``. The URL is the whole
credential, so it lives in /log/keybudget.env and never in the repo. An unset
variable disables that heartbeat.

**Monitoring must never break the thing it monitors.** Every failure here is
swallowed. A guard that dies because Kuma was unreachable is worse than no
guard. If Kuma cannot be reached its monitor goes DOWN for want of a heartbeat,
which is the correct signal anyway.

stdlib only: this is deployed beside fanotify_guard.py in a container with no
pip packages.
"""

from __future__ import annotations

import urllib.parse

# nosemgrep rationale: no-urllib-on-main-loop guards the orchestrator's asyncio
# loop (TM-015, ADR-0001), where a blocking call starves every other task. This
# module runs in the cache-catcher container, which has no event loop, no
# asyncio and no pip packages -- httpx.AsyncClient is not installable there. The
# call blocks one dedicated daemon thread for at most TIMEOUT_SEC.
#
# Both spellings of the id are listed because CI pins semgrep 1.36.0, which
# matches only the fully-qualified `semgrep.`-prefixed form -- the namespace
# comes from loading the rules out of `.semgrep/` -- while the 1.175 used
# locally accepts the bare one. An id that matches nothing is ignored, so
# listing both works on either version and survives a CI upgrade.
import urllib.request  # nosemgrep: semgrep.no-urllib-on-main-loop,no-urllib-on-main-loop

# Kuma stores the message and shows it on the monitor. An unbounded blob does not
# belong in a URL, and the useful part of a failure is its tail.
MSG_MAX_CHARS = 200

TIMEOUT_SEC = 10.0


def push(url, status, msg="", opener=None):
    """Send one heartbeat. Never raises. Reports whether it was delivered.

    Args:
        url: the monitor's push URL, or None/blank to disable this heartbeat.
        status: ``"up"`` or ``"down"``.
        msg: short human-readable detail, truncated to ``MSG_MAX_CHARS``.
        opener: injected by tests; production passes nothing.

    Returns:
        True if the monitor accepted the push, or if no URL is configured -- a
        deliberate disable is nothing to retry. False only when a send was
        attempted and did not arrive.
    """
    if not url or not url.strip():
        return True

    trimmed = msg[-MSG_MAX_CHARS:] if len(msg) > MSG_MAX_CHARS else msg
    query = urllib.parse.urlencode({"status": status, "msg": trimmed})
    target = f"{url.strip()}?{query}"

    try:
        open_url = opener or urllib.request.urlopen
        with open_url(target, timeout=TIMEOUT_SEC) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:
        return False
