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
# containing these gets redacted) and letter-bounded short tokens (bounded to
# avoid matching "saltwater" etc.). Case-insensitive.
#
# Substring patterns are chosen over exhaustive word-boundary variants because
# over-redaction is preferable to under-redaction for credential-handling code.
#
# Short tokens use letter-class boundaries `(?:^|[^a-zA-Z])...(?:[^a-zA-Z]|$)`
# rather than `\b...\b` because Python's `\b` uses `\w` boundaries, and `_` is
# a `\w` character — so `\b` does NOT fire between `_` and `pin` in `user_pin`.
# The original `\b(?:pin|...)\b` pattern silently failed on every compound key
# of the form `user_pwd`, `my_pin`, `otp_code`, `creds_list`, etc. Letter-class
# boundaries treat underscore, digit, and separator as valid boundaries.
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
    r"(?:^|[^a-zA-Z])(?:pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce)(?:[^a-zA-Z]|$)",
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
    """Bind a correlation_id for the duration of the block; restore prior
    contextvars on exit.

    Exception-safe: the finally block runs even if the body raises. Nested
    usage restores the enclosing context's correlation_id on inner exit
    (rather than wiping everything), via structlog's token-based reset.

    Use this at request / job entrypoints rather than the raw
    bind_correlation_id() + clear_request_context() pair, which is easy to
    forget and leaks across requests when pooled workers reuse threads.

    Usage:
        with request_context() as cid:
            log.info("handling_request", cid=cid)
    """
    cid = correlation_id or new_correlation_id()
    tokens = structlog.contextvars.bind_contextvars(correlation_id=cid)
    try:
        yield cid
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


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

    Collision handling: if `user_<key>` is ALREADY present in the event_dict
    (caller genuinely set that field), the rescued value is stashed at
    `user_<key>_2`, `user_<key>_3`, etc. — never silently overwrites a
    pre-existing user field.

    No-op for non-reserved keys and for reserved keys not currently bound.
    """
    ctx = structlog.contextvars.get_contextvars()
    for key in RESERVED_KEYS & ctx.keys():
        if key in event_dict and event_dict[key] != ctx[key]:
            target = f"user_{key}"
            if target in event_dict:
                i = 2
                while f"{target}_{i}" in event_dict:
                    i += 1
                target = f"{target}_{i}"
            event_dict[target] = event_dict[key]
            event_dict[key] = ctx[key]
    return event_dict


def _redact_sensitive_values(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Recursively mask values of keys matching `_SENSITIVE_KEY_RE` with the
    redaction marker. Walks nested dicts, lists, and tuples. Non-string keys
    are coerced to str for matching.

    Cycle-safe: tracks visited container ids in a seen-set and substitutes
    the string `"<cyclic>"` on repeat. Prevents `RecursionError` when a caller
    logs a self-referential structure (ORM backrefs, hand-rolled graphs)."""

    def _is_asgi_headers_shape(obj: Any) -> bool:
        """Detect the ASGI headers shape: a non-empty list/tuple where every
        element is a 2-tuple whose first item is bytes-or-str. UAT-3 S3-k —
        without this, logging `scope=scope` would bypass redaction because
        `scope["headers"]` is a list of (bytes, bytes) tuples, and the regex
        only walks dict KEYS."""
        if not isinstance(obj, (list, tuple)) or not obj:
            return False
        return all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], (bytes, str))
            for item in obj
        )

    def _walk(obj: Any, seen: frozenset[int]) -> Any:
        if isinstance(obj, (dict, list, tuple)):
            if id(obj) in seen:
                return "<cyclic>"
            seen = seen | {id(obj)}
        if isinstance(obj, dict):
            return {
                k: (_REDACTION_MARKER if _SENSITIVE_KEY_RE.search(str(k)) else _walk(v, seen))
                for k, v in obj.items()
            }
        if _is_asgi_headers_shape(obj):
            redacted_headers: list[tuple[Any, Any]] = []
            for k, v in obj:
                key_str = k.decode("ascii", errors="ignore") if isinstance(k, bytes) else k
                if _SENSITIVE_KEY_RE.search(key_str):
                    redacted_headers.append((k, _REDACTION_MARKER))
                else:
                    redacted_headers.append((k, _walk(v, seen)))
            return type(obj)(redacted_headers) if isinstance(obj, tuple) else redacted_headers
        if isinstance(obj, (list, tuple)):
            return type(obj)(_walk(x, seen) for x in obj)
        return obj

    return cast("MutableMapping[str, Any]", _walk(event_dict, frozenset()))


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
