"""F7 startup self-test gating health.validator_healthy.

Catches deployment misconfiguration (cache not mounted / wrong path /
unreadable) before the validator accepts work. A failed self-test sets
`app.state.validator_healthy = False`, which forces `/health` to 503 until
the issue is fixed and the process restarted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from orchestrator.validator.cache_key import cache_key, cache_path

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)


async def validator_self_test(
    settings: Settings, *, agent_client: AgentClient | None = None
) -> bool:
    """Return True iff the cache mount is usable and key derivation runs.

    re-arch ④: when `settings.agent_enabled` and an agent client is supplied,
    validator health is sourced from the AGENT (which owns the cache mount),
    because the control plane now runs on an LXC with no local cache mount —
    stat'ing `lancache_nginx_cache_path` there would always (wrongly) report
    unhealthy. With the flag off (default) the behaviour below is unchanged: the
    local cache path is the source of truth and `agent_client` is ignored.

    1. `lancache_nginx_cache_path` must be an existing, listable directory.
    2. The cache-key derivation must run without error (exercises the
       `cache_key` module end-to-end, no I/O).
    """
    if settings.agent_enabled and agent_client is not None:
        try:
            body = await agent_client.agent_health()
        except Exception as e:
            _log.error("validator.self_test.agent_unreachable", reason=str(e)[:200])
            return False
        return bool(body.get("validator_healthy", False))

    root = Path(settings.lancache_nginx_cache_path)
    try:
        if not root.is_dir():
            _log.error("validator.self_test.cache_root_missing", path=str(root))
            return False
        # Confirm read access (raises if unreadable).
        first_entry = next(iter(root.iterdir()), None)
        # An unmounted Docker bind-mount/volume silently becomes an EMPTY,
        # non-mountpoint directory (Docker auto-creates the target). is_dir() +
        # iterdir() both succeed, so the validator would run against the wrong
        # empty tree and report every cached game as missing — exactly the
        # operator-confusion this self-test exists to prevent (audit 2026-06-09).
        # A correctly-mounted-but-fresh cache is still a mountpoint, so only the
        # empty-AND-not-mounted combination is rejected.
        if first_entry is None and not os.path.ismount(root):
            _log.error("validator.self_test.cache_empty_and_unmounted", path=str(root))
            return False
        # Derivation smoke test — synthetic, never touches disk.
        h = cache_key(settings.steam_cache_identifier, "/depot/0/chunk/" + "0" * 40, "bytes=0-0")
        cache_path(root, h, settings.cache_levels)
        _log.info("validator.self_test.ok", path=str(root))
        return True
    except OSError as e:
        _log.error("validator.self_test.failed", reason=str(e)[:200])
        return False
