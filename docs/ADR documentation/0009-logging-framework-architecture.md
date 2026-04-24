# ADR-0009: Logging Framework Architecture — Scoped Context, Reserved-Key Protection, Secret Redaction

**Status:** Accepted
**Date:** 2026-04-22
**Phase:** 2 (Construction), Milestone B, Build Loop 2 (ID3)
**Supersedes:** Earlier ID3 implementation (commit `deec8c9`, rolled back 2026-04-22)
**Related:** ADR-0001 (Orchestrator Architecture), ADR-0008 (Migration Runner)
**Feature:** ID3-structured-logging

<!-- Last Updated: 2026-04-22 -->

## Context

ID3 (the structured-logging framework) originally shipped on 2026-04-20 as a
41-line module wrapping structlog with a basic JSON pipeline. UAT Session 1
(2026-04-22) found 4 bugs: 2 SEV-2, 2 SEV-3 (GH issues #9, #10, #14, #15).
The BL2 rewrite had to close all of them test-first.

This ADR records the four load-bearing architectural decisions behind the
rewrite, plus the one notable edge-case resolution.

## Decisions

### D1 — Scoped correlation-ID binding via `request_context()` + token reset

**Context:** The original `bind_correlation_id()` + `clear_request_context()`
primitives were advisory — callers had to remember to clear the binding in
a `finally` block. Any handler that forgot (or threw an exception before the
finally) leaked the CID across pooled workers. The audit persona's first
proposed fix was a plain context manager that called `clear_contextvars()`
on exit — which the re-audit then rejected because nested contexts would
lose the enclosing block's CID.

**Decision:** Use structlog's token-based reset. `request_context()` calls
`structlog.contextvars.bind_contextvars(correlation_id=cid)`, which returns
a dict of `contextvars.Token` objects. The `finally` block calls
`structlog.contextvars.reset_contextvars(**tokens)`, which restores each
variable to its **previous** value (or unsets it if it was previously unbound).
Nested `request_context()` blocks now correctly restore the outer CID on
inner exit.

**Consequence:** Callers write:
```python
with request_context() as cid:
    log.info("handling_request")
```
and never see stale CIDs across requests. The low-level `bind_correlation_id()`
/ `clear_request_context()` primitives remain for unusual cases, with
docstrings steering new callers toward `request_context()`.

**Trade-off:** The primitives and the context manager are now inconsistent
— the primitives still wipe all contextvars on clear, while the context
manager restores. Acceptable because `clear_request_context()` is marked as
low-level in the docstring.

### D2 — `_protect_reserved_keys` processor with collision-aware rename

**Context:** structlog's `merge_contextvars` uses `ctx.update(event_dict)` —
user kwargs silently override contextvars. A caller passing a
`correlation_id` kwarg would replace the real CID in log output with the
user-supplied string, defeating audit trails. The initial
fix just wrote `event_dict[f"user_{key}"] = event_dict[key]`, which the
re-audit flagged as silently overwriting legitimate pre-existing
`user_correlation_id` fields.

**Decision:** Add a processor **after** `merge_contextvars` that:
1. For each key in `RESERVED_KEYS` (correlation_id, level, timestamp, event,
   logger, logger_name) that is currently bound in contextvars AND has a
   conflicting value in event_dict,
2. Check whether `user_<key>` is already present in event_dict. If yes, walk
   `user_<key>_2`, `_3`, … until a free slot is found.
3. Move the user's value to that slot, restore the contextvars value.

The `RESERVED_KEYS` set is exported as a public constant so callers can
introspect.

**Consequence:** User kwargs that collide with framework keys are rescued,
never dropped and never silently overwritten. Collision cases (caller passes
both `correlation_id=X` AND `user_correlation_id=Y`) produce a deterministic
numbered slot for every user-supplied value.

**Trade-off:** `event` is not actually protected by this processor — structlog
raises `TypeError` at the Python call-site level because its method signature
is `meth(event, **kw)`. That's stricter than our rescue, which is fine.

### D3 — `_redact_sensitive_values` processor with letter-class boundaries

**Context:** The orchestrator handles Steam and Epic credentials. Any
`log.info("auth", password=...)` that serialized the secret verbatim to
stdout → container logs → log aggregator is the primary credential-leak
vector in the project's threat model. The initial fix added a recursive
regex-based redactor, but the regex used `\b(?:pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce)\b`
to bound short tokens. The re-audit caught that `\b` in Python regex uses
`\w` boundaries, and `_` is a `\w` character — so NO boundary fires between
`_` and a short token. Compound keys like `user_pwd`, `my_pin`, `otp_code`,
`creds_list`, `nonce_bytes`, `salt_value`, `user_sid` all leaked
credentials in plain text. Live reproduction confirmed the leak before ship.

**Decision:** Replace `\b…\b` with letter-class boundaries
`(?:^|[^a-zA-Z])(?:pwd|pin|…)(?:[^a-zA-Z]|$)`. Underscore, digit, hyphen,
and start/end-of-string all count as valid boundaries; adjacent letters do
not. Every credential-shaped compound key is now caught; `pinnacle`,
`spinner`, `pinhead`, `saltwater` pass through unchanged.

For the longer substring patterns (password, token, secret, authorization,
bearer, cookie, session, api_key, credential, private_key, signature) the
regex intentionally matches anywhere in the key — aggressive over-redaction
is strictly preferable to under-redaction for credential-handling code.

**Consequence:** Any log-event key matching any pattern has its value replaced
with the constant `<redacted>` marker. The processor walks nested dicts,
lists, and tuples. Cycle-safe: a `frozenset[int]` seen-set substitutes
`"<cyclic>"` on repeat, preventing `RecursionError` when a caller logs a
self-referential structure.

**Trade-off:** Value-content scanning (e.g., detecting a bearer token
embedded in a string) is NOT implemented. Key-based redaction relies on
callers using descriptive field names. Acceptable because callers in this
project are all first-party and audited; the 16 GitHub issues closed by BL1
+ BL2 already establish the key-naming discipline.

### D4 — Strict `log_level` validation

**Context:** The original `getattr(logging, log_level.upper(), logging.INFO)`
silently coerced typos (`WARN`, `VERBOSE`, `TRACE`) to INFO. Operators would
ship a misconfigured deployment and only discover it at incident time when
expected noise wasn't in the logs.

**Decision:** Validate `log_level` against `{DEBUG, INFO, WARNING, ERROR,
CRITICAL}` (case-insensitive after `strip().upper()`). Raise `ValueError`
on anything else. No silent fallback.

**Consequence:** An operator typo surfaces at `configure_logging()` time
(container startup), not at incident time. Failure is loud and unambiguous.

**Trade-off:** A deployment that relied on the previous forgiving behavior
would now fail to start. Accepted — this is a security-sensitive correctness
fix, not a backward-compatibility concern.

## Processor pipeline (final order)

```
merge_contextvars            # structlog built-in: merge contextvars + event_dict
_protect_reserved_keys       # NEW — restore reserved vars after user-kwarg merge
stdlib.add_log_level         # structlog built-in: set event_dict["level"]
StackInfoRenderer            # structlog built-in: render stack info
dev.set_exc_info             # structlog built-in: attach exc_info from sys
TimeStamper(fmt="iso", utc)  # structlog built-in: add "timestamp" key
format_exc_info              # structlog built-in: format traceback string
_redact_sensitive_values     # NEW — redact before JSON serialization
JSONRenderer                 # structlog built-in: final JSON string
```

## Consequences

- ID3 closes 4 audit findings + 4 re-audit hardening items test-first.
- Future callers should use `request_context()` for correlation-ID scoping
  rather than the low-level primitives.
- Log-event keys should be descriptive (`user_password`, `auth_token`,
  not `p`, `tk`) to ensure the redactor catches them.
- The Steam and Epic adapters (F1, F2) must verify their logging paths do
  not stringify credentials into values — the redactor is a safety net,
  not a substitute for careful field naming.

## Related work

- Commit `15203c6` — initial rewrite closing issues #9, #10, #14, #15
- Commit `13e0843` — hardening pass closing re-audit N1, N2, N3, N4
- Audit artifact: `docs/security-audits/id3-structured-logging-security-audit.md`
- Follow-up issue: [#22](https://github.com/kraulerson/lancache-orchestrator/issues/22) (N5 perf short-circuit, SEV-4)
