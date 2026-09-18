"""Who deleted a cache file — commanded purge, nginx eviction, or unknown (#337).

cache-catcher watches the lancache filesystem and emails when cached game files
disappear. It captures the deleting process on every event and, until this
module existed, ignored it: an operator-initiated purge produced the same
emergency alert as nginx's cache-manager evicting from a full keys_zone.

Kept separate from `fanotify_guard.py` on purpose. The guard loads `libc.so.6`
at import time and cannot run anywhere but the NAS container, so any logic
inside it is untestable in CI. This module is stdlib-only and pure.

**Bias: unknown alerts.** Only a deleter positively identified as the
orchestrator agent is treated as commanded. Everything else — nginx, nfsd, a
stray `rm`, a process whose /proc entry vanished before the event was read —
counts toward the eviction signature. A monitor that guesses "probably fine"
is worse than no monitor, because the 2026-07-31 mass deletion ran for days
without anyone noticing.
"""

from __future__ import annotations

ACTOR_ORCHESTRATOR = "orchestrator"
ACTOR_NGINX = "nginx"
ACTOR_OTHER = "other"

# The orchestrator agent is launched as `python -m orchestrator.agent`, so the
# module invocation appears verbatim in the process cmdline. Matching on that
# exact token — rather than the bare word "orchestrator" — is deliberate: a
# stray `rm -rf /opt/orchestrator-backup` must NOT be able to silence the alarm
# by having the word in its path. Precision beats recall here, because a false
# negative turns the monitor off in precisely the case it exists for.
_ORCHESTRATOR_INVOCATION = "-m orchestrator.agent"


def classify_actor(comm: str | None = None, exe: str | None = None, cmd: str | None = None) -> str:
    """Identify the deleting process from the details fanotify gives us.

    Args:
        comm: the kernel's short process name, e.g. ``nginx`` or ``python``.
        exe: the resolved executable path, may be empty.
        cmd: the full cmdline, may be empty if the process already exited.

    Returns:
        ``ACTOR_ORCHESTRATOR`` only on a positive match, ``ACTOR_NGINX`` for
        nginx's own maintenance, ``ACTOR_OTHER`` for anything else including
        unattributable events.
    """
    cmdline = cmd or ""
    if _ORCHESTRATOR_INVOCATION in cmdline:
        return ACTOR_ORCHESTRATOR
    if (comm or "").strip() == "nginx":
        return ACTOR_NGINX
    return ACTOR_OTHER


def counts_toward_eviction(actor: str) -> bool:
    """Whether this delete should feed the mass-eviction counter.

    A commanded purge is excluded because it is a deliberate operator action the
    orchestrator has already recorded in ``measurement_transitions`` with
    ``commanded = 1`` — the same distinction the circuit breaker makes, and for
    the same reason. Everything else counts.
    """
    return actor != ACTOR_ORCHESTRATOR
