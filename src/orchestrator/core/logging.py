"""Structured logging for the orchestrator (ID3).

See `tests/core/test_logging.py` for the contract.

- JSON output via structlog, one event per line on stdout
- Correlation-ID tracking via contextvars, scoped by `request_context()`
  which clears even on exception (supersedes the raw bind/clear pair)
- Reserved-key protection: user kwargs that collide with contextvars-
  owned keys (correlation_id, level, timestamp, event, logger, logger_name)
  are rescued to `user_<key>` rather than silently overriding
- Secret redaction: keys matching common credential patterns have their
  values replaced with `<redacted>`; recursive through dicts + lists
- log_level is validated against the stdlib set; raises `ValueError` on
  anything unknown (no silent fallback to INFO)
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

RESERVED_KEYS: frozenset[str] = frozenset(
    {"correlation_id", "level", "timestamp", "event", "logger", "logger_name"}
)

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_REDACTION_MARKER = "<redacted>"

# Sensitive-key matcher. Union of substring patterns (aggressive — any key
# containing these gets redacted) and word-bounded short tokens (bounded to
# avoid matching "saltwater" etc.). Case-insensitive.
#
# Substring patterns are chosen over exhaustive word-boundary variants because
# over-redaction is preferable to under-redaction for credential-handling code.
_SENSITIVE_KEY_RE = re.compile(
    r"password|passwd|passphrase|"
    r"token|jwt|"
    r"secret|"
    r"authorization|bearer|"
    r"cookie|"
    r"session|"
    r"api[_-]?key|apikey|"
    r"credential|"
    r"private[_-]?key|privkey|"
    r"signature|"
    # Short ambiguous tokens need word boundaries to avoid false positives
    # (e.g., "pinnacle" must NOT match "pin"). `\b` here uses \w boundaries;
    # `_pin_` triggers at either underscore.
    r"\b(?:pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Correlation-ID primitives + scoped context manager (GH issue #9)
# ---------------------------------------------------------------------------


def new_correlation_id() -> str:
    """Generate a new UUID4 correlation ID (hex, no dashes)."""
    return uuid.uuid4().hex


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind `correlation_id` into the current contextvars scope.

    Low-level primitive — prefer `request_context()` which also clears the
    binding automatically, including on exception.
    """
    cid = correlation_id or new_correlation_id()
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def clear_request_context() -> None:
    """Clear all contextvars-bound values in the current scope.

    Low-level primitive — prefer `request_context()`.
    """
    structlog.contextvars.clear_contextvars()


@contextmanager
def request_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation_id for the duration of the block; clear on exit.

    Exception-safe: the finally block clears contextvars even if the body
    raises. Use this at request / job entrypoints rather than the raw
    bind_correlation_id() + clear_request_context() pair, which is easy to
    forget and leaks across requests when pooled workers reuse threads.

    Usage:
        with request_context() as cid:
            log.info("handling_request", cid=cid)
    """
    cid = bind_correlation_id(correlation_id)
    try:
        yield cid
    finally:
        clear_request_context()


# ---------------------------------------------------------------------------
# Processors (run in this order inside the structlog pipeline)
# ---------------------------------------------------------------------------


def _protect_reserved_keys(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Rescue reserved keys that user kwargs would otherwise clobber.

    structlog's `merge_contextvars` runs before us and uses `ctx.update(event_dict)`
    — user kwargs override contextvars. For reserved keys we invert that: if the
    key is bound in contextvars and the event_dict has a different value, restore
    the contextvars value and save the user's under `user_<key>`.

    No-op for non-reserved keys and for reserved keys not currently bound.
    """
    ctx = structlog.contextvars.get_contextvars()
    for key in RESERVED_KEYS & ctx.keys():
        if key in event_dict and event_dict[key] != ctx[key]:
            event_dict[f"user_{key}"] = event_dict[key]
            event_dict[key] = ctx[key]
    return event_dict


def _redact_sensitive_values(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Recursively mask values of keys matching `_SENSITIVE_KEY_RE` with the
    redaction marker. Walks nested dicts, lists, and tuples. Non-string keys
    are coerced to str for matching."""

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (_REDACTION_MARKER if _SENSITIVE_KEY_RE.search(str(k)) else _walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return type(obj)(_walk(x) for x in obj)
        return obj

    return cast("MutableMapping[str, Any]", _walk(event_dict))


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------


def configure_logging(*, log_level: str = "INFO") -> None:
    """Configure structlog for JSON output with the orchestrator's processor chain.

    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL
            (case-insensitive). Raises `ValueError` on any other value —
            no silent fallback to INFO, so operator typos surface immediately.
    """
    normalized = log_level.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}",
        )
    numeric_level = getattr(logging, normalized)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _protect_reserved_keys,
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact_sensitive_values,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
