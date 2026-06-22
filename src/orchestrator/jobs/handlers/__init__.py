"""Handler registry for the jobs worker (BL11).

Built-in handlers (e.g. `library_sync`) auto-register on first import of
this package; tests that exercise the worker loop typically call
`clear()` first and register stubs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

Handler = Callable[[dict[str, Any], "Deps"], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    """Register a handler under a `jobs.kind` value. Idempotent re-registration
    overwrites — tests use this when swapping a real handler for a stub.
    """
    HANDLERS[kind] = handler


def clear() -> None:
    """Test helper: empty the registry."""
    HANDLERS.clear()


def _register_builtin_handlers() -> None:
    """Wire built-in handlers at import time."""
    from orchestrator.jobs.handlers.library_sync import library_sync_handler
    from orchestrator.jobs.handlers.prefill import prefill_handler
    from orchestrator.jobs.handlers.sweep import sweep_handler
    from orchestrator.jobs.handlers.validate import validate_handler

    register("library_sync", library_sync_handler)
    register("prefill", prefill_handler)
    register("sweep", sweep_handler)
    register("validate", validate_handler)


_register_builtin_handlers()
