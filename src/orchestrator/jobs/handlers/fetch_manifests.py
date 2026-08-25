"""fetch_manifests job handler — trigger the agent's DepotDownloader manifest-only
fetch (closes the validation-coverage gap). The agent self-enumerates the cached
app set; this handler dispatches, logs the tally, and hands it back so the worker's
Uptime Kuma heartbeat can carry it.

UAT-14 #294: the tally used to be logged and discarded, so the job recorded
`succeeded` while 669 of 1170 apps failed and the failures lived only in the agent's
warning stream. The job state is still `succeeded` — `succeeded` means the work ran,
and per-app failures are data rather than a job fault — but the numbers now reach a
human, and the heartbeat goes DOWN when the failure ratio crosses a threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.summary import JobSummary

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


def summarise_tally(tally: dict[str, Any], max_failure_ratio: float) -> tuple[bool, str]:
    """Turn the agent's tally into an up/down verdict and a one-line summary.

    The threshold is a RATIO of apps, not an absolute count, so a big library and a
    small one alarm at the same proportion.

    A missing or malformed tally reads as healthy: the agent's response shape is not
    ours to guarantee, and inventing a failure from an unexpected payload would be
    its own false alarm.
    """
    apps = int(tally.get("apps") or 0)
    failed = int(tally.get("failed") or 0)
    fetched = int(tally.get("fetched") or 0)
    skipped = int(tally.get("skipped") or 0)

    msg = f"apps={apps} fetched={fetched} skipped={skipped} failed={failed}"

    if apps <= 0:
        return True, msg

    return (failed / apps) <= max_failure_ratio, msg


async def fetch_manifests_handler(job: dict[str, Any], deps: Deps) -> JobSummary:
    """Dispatch a manifest-only fetch to the data-plane agent.

    Returns:
        The tally as a JobSummary, for the worker's heartbeat.

    Raises:
        ValueError — no agent client configured (agent_enabled off).
    """
    if deps.agent_client is None:
        raise ValueError("fetch_manifests requires the data-plane agent (agent_enabled)")

    result = await deps.agent_client.fetch_manifests()
    _log.info("fetch_manifests.done", job_id=job.get("id"), **result)

    ok, msg = summarise_tally(result, get_settings().fetch_manifests_max_failure_ratio)
    if not ok:
        _log.warning("fetch_manifests.failure_ratio_exceeded", job_id=job.get("id"), **result)

    return JobSummary(ok=ok, msg=msg)
