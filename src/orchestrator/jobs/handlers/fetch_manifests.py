"""fetch_manifests job handler — trigger the agent's DepotDownloader manifest-only
fetch (closes the validation-coverage gap). The agent self-enumerates the cached
app set; this handler just dispatches and logs the tally."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


async def fetch_manifests_handler(job: dict[str, Any], deps: Deps) -> None:
    """Dispatch a manifest-only fetch to the data-plane agent.

    Raises:
        ValueError — no agent client configured (agent_enabled off).
    """
    if deps.agent_client is None:
        raise ValueError("fetch_manifests requires the data-plane agent (agent_enabled)")
    result = await deps.agent_client.fetch_manifests()
    _log.info("fetch_manifests.done", job_id=job.get("id"), **result)
