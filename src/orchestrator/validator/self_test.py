"""F7 startup self-test gating health.validator_healthy.

Catches deployment misconfiguration (cache not mounted / wrong path /
unreadable) before the validator accepts work. A failed self-test sets
`app.state.validator_healthy = False`, which forces `/health` to 503 until
the issue is fixed and the process restarted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from orchestrator.validator.cache_key import cache_key, cache_path

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)


async def validator_self_test(settings: Settings) -> bool:
    """Return True iff the cache mount is usable and key derivation runs.

    1. `lancache_nginx_cache_path` must be an existing, listable directory.
    2. The cache-key derivation must run without error (exercises the
       `cache_key` module end-to-end, no I/O).
    """
    root = Path(settings.lancache_nginx_cache_path)
    try:
        if not root.is_dir():
            _log.error("validator.self_test.cache_root_missing", path=str(root))
            return False
        # Confirm read access (raises if unreadable).
        next(iter(root.iterdir()), None)
        # Derivation smoke test — synthetic, never touches disk.
        h = cache_key(settings.steam_cache_identifier, "/depot/0/chunk/" + "0" * 40, "bytes=0-0")
        cache_path(root, h, settings.cache_levels)
        _log.info("validator.self_test.ok", path=str(root))
        return True
    except OSError as e:
        _log.error("validator.self_test.failed", reason=str(e)[:200])
        return False
