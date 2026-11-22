"""T1 trigger: enqueue a ``new_games_check`` when SteamPrefill's downloaded
record gains entries.

The baseline lives in memory only. The first successful poll records it and
never enqueues. An agent error or an empty ``{}`` (e.g. a restarting agent)
leaves the baseline untouched, so the next good poll does not see the whole
library as newly added. A poll that sees added entries moves the baseline only
when its enqueue inserted a check, so a deduped or failed enqueue is re-tried on
the next poll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from orchestrator.scheduler.jobs import enqueue_new_games_check

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


class SteamDownloadWatch:
    def __init__(self, pool: Pool, agent_client: AgentClient) -> None:
        self._pool = pool
        self._agent = agent_client
        self._baseline: frozenset[tuple[str, int]] | None = None

    async def poll(self) -> bool:
        """Return True when this poll enqueued a check. Never raises."""
        try:
            state = await self._agent.downloaded_state()
        except Exception as e:
            _log.warning(
                "new_games.download_watch_failed",
                error=type(e).__name__,
                reason=str(e)[:200],
            )
            return False
        if not state:
            _log.warning("new_games.download_watch_empty")
            return False

        current = frozenset((key, value) for key, values in state.items() for value in values)
        baseline = self._baseline
        if baseline is None:
            self._baseline = current
            _log.info("new_games.download_watch_baseline", entries=len(current))
            return False
        added = current - baseline
        if not added:
            self._baseline = current  # removed-only or unchanged: nothing to check
            return False
        # Move the baseline only when a check was actually queued. A dedup (a check
        # already in flight may have read its check set before this download landed)
        # or a DB error returns 0; keeping the baseline makes the next poll re-try.
        inserted = await enqueue_new_games_check(self._pool, source="trigger")
        if inserted:
            self._baseline = current
        _log.info("new_games.download_watch_added", added=len(added), queued=bool(inserted))
        return bool(inserted)
