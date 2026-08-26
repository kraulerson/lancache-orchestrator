"""What a job handler can tell the monitor about how it went.

Handlers return ``None`` by default: the job's outcome is success unless it raised,
and that is unchanged. A handler that knows more than "it did not throw" — a tally of
per-item results, say — returns a JobSummary instead, and the worker puts it in the
Uptime Kuma heartbeat.

This deliberately does NOT feed back into ``jobs.state``. A partially-failing run is
still a run that happened; conflating "some items failed" with "the job broke" makes
`failed` useless as a signal (UAT-14 #294).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobSummary:
    """A handler's own verdict on a run, for monitoring only.

    Attributes:
        ok: whether the heartbeat should report up. False pushes down without
            changing the job's recorded state.
        msg: one short human-readable line, shown on the Kuma monitor.
    """

    ok: bool
    msg: str
