# Security Audit — ID4 Settings Module

**Feature:** ID4-settings (Build Loop 3, Milestone B)
**Module:** `src/orchestrator/core/settings.py`
**Audit date:** 2026-04-23
**Auditor personas:** three parallel sub-agents — Senior Security Engineer (SAST), Penetration Tester (threat-model cross-check), Malicious User (input/redaction)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-04-23 -->

## Scope

Post-implementation security review of the new ID4 settings module, covering:

- `src/orchestrator/core/settings.py` (16 typed fields, 4 diagnostic warnings, `@lru_cache` singleton, `__reduce__` block, `__init__` scrubbing wrapper)
- `tests/core/test_settings.py` (67 tests including 2 SEV-2 regression tests)
- `tests/core/conftest.py` (autouse `_isolated_env` fixture)

## Methodology

Three parallel sub-agents dispatched per CLAUDE.md "Multi-Agent Parallelism":

1. **SAST.** `semgrep --config=auto` + project custom rules (`.semgrep/`), plus manual hard-stop checklist (token leak paths, env → eval injection, ReDoS on `cache_levels` regex, SQL injection vectors, path traversal via `secrets_dir` / `database_path`).
2. **Threat-model cross-check.** Against `docs/phase-1/threat-model.md` TM-001 (bearer leak via Game_shelf `.env`), TM-012 (log redaction), TM-023 (kill chain). Also inspected how the four diagnostic warnings serialize through ID3's redaction pipeline.
3. **Input validation + redaction.** Env-var injection (shell metacharacters, path traversal, JSON bombs, integer overflow), ReDoS timing on `cache_levels` with 50K-char input, empirical `SecretStr` redaction test across `repr`, `model_dump`, `model_dump(mode='json')`, `json.dumps`, `pickle`, `model_json_schema`, plus `ValidationError.input_value` echo check.

All findings verified empirically by the Orchestrator before triage.

## Audit findings

| # | Severity | Title | Status |
|---|---|---|---|
| A1 | SEV-2 | **`pickle.dumps(Settings(...))` leaks raw token** via `SecretStr._secret_value` (no `__reduce__` override on `SecretStr`). No current code path pickles Settings, but the primitive itself is broken; any future DX sugar (multiprocessing task args, on-disk cache, Celery) would write cleartext to attacker-readable storage. | **FIXED** — `Settings.__reduce__` raises `TypeError("Settings is not pickle-safe — re-read via get_settings()")`. Regression test `test_settings_not_pickleable`. |
| A2 | SEV-2 | **`ValidationError` echoes raw rejected token** in `input_value` field. A rotation-failure startup (operator writes 31-char token) would emit `ValidationError` with `input_value='<candidate_token>'` to the systemd journal. | **FIXED** — `Settings.__init__` intercepts pydantic's `ValidationError`, filters errors whose `loc` includes `"token"`, and re-raises as `ValueError` with a scrubbed message. Non-token field errors propagate unchanged. Regression test `test_short_token_validation_error_does_not_echo_raw`. |

## Non-findings (explicitly checked, clean)

- **Token leak via `repr`, `model_dump`, `model_dump(mode="json")`, `json.dumps`.** Five-shape parametrized tests (alphanumeric / hex / base64-padded / embedded-newline / unicode × 3 serialization forms = 15 assertions) confirm no leak. `pydantic.SecretStr`'s censoring holds across all standard paths.
- **ReDoS on `cache_levels` regex `^\d+(:\d+)*$`.** Empirical test: 50 000-char input rejected in **0.0007 s**. No catastrophic backtracking — literal `:` separator eliminates alternative overlap, and pydantic v2 uses Rust's `regex` crate (non-backtracking anyway).
- **Env-var → eval/exec injection.** No `eval`, `exec`, `subprocess`, or `os.system` imported. `os.environ` is only read via `in` membership check in `_emit_config_warnings`.
- **SQL injection.** No SQL driver imported, no query composition.
- **Path traversal via `secrets_dir`.** Hardcoded to `/run/secrets` in `model_config`; not operator-controllable from env.
- **Path traversal via `database_path`, `steam_session_path`, `epic_session_path`, `lancache_nginx_cache_path`.** Module only stores `Path` objects, never opens. Consumer-responsibility boundary documented in ADR-0010.
- **Integer overflow on sized fields.** `api_port`, `cache_slice_size_bytes`, `manifest_size_cap_bytes`, `chunk_concurrency`, `steam_upstream_silent_days` all enforce `ge`/`le`/`gt` bounds via `Field()`; negative / zero / over-max values rejected cleanly.
- **Enum escape on `log_level`, `require_local_fs`.** `Literal` constrained; case-sensitive; invalid values rejected.
- **`cors_origins` injection.** Empty-string element rejected via `@field_validator`. JSON-array parsing from env is pydantic-settings default behavior.
- **TM-001 (bearer-token leak).** `orchestrator_token` is `SecretStr` throughout; `_strip_token` re-wraps in `SecretStr` after stripping; never unwrapped except via explicit `.get_secret_value()`. No leak path in BL3 scope.
- **TM-012 (log redaction).** All four warning events flow through ID3's `_redact_sensitive_values`. The `secret_file=<path>` kwarg in the shadow warning matches ID3's `secret` key pattern and is auto-redacted to `<redacted>`. Since the path is a deployment invariant (`/run/secrets/orchestrator_token`), over-redaction doesn't blunt diagnostic value.
- **TM-023 (kill chain).** ID4 is a config loader — no endpoints, no CDN calls, no secret persistence. Bible §7.3's container-refuses-start-on-missing invariant is enforced via pydantic's required-field mechanic (`Field(...)`) + the `__init__` scrubbing wrapper.
- **Symlink-follow in shadow check.** `secret_file.is_file()` follows symlinks. Exploitation requires write access to `/run/secrets`, which is already a container-root compromise. Not a practical SEV.
- **`extra="ignore"`.** Unknown `ORCH_*` env vars silently ignored — no typo-squatting DoS, no injection of unknown fields that later code reads unsafely. Correct default.
- **`model_json_schema()` leak.** Schema reports `format: password`, `writeOnly: true`; no default value for `orchestrator_token` to leak.

## Agent triage and resolution

- **SAST:** 0 findings at SEV-3 or above. Cleared.
- **Threat-model:** 0 findings. Cleared. SEV-4 UX nit on shadow-warning kwarg name (`secret_file` → over-redacted to `<redacted>`) — accepted as a feature since the path is a deployment invariant.
- **Input/redaction:** 2 SEV-2 findings (A1, A2). Initial severity classification by the agent was SEV-1 for A1; Orchestrator downgraded to SEV-2 because no current exploit path exists (nothing pickles Settings in BL3 or any planned consumer) — the primitive is faulty but unexploited. Both fixed test-first in this BL.

## Tooling hygiene follow-ups (SEV-4, not audit findings)

- **Bandit not installed in venv.** Phase 2.4 checklist expects bandit availability; recommend adding to dev dependencies.
- ~~**Bible §10.3 drift.**~~ **Retracted 2026-04-24 (#26 triage).** Re-reading Bible §10.3, it lists 7 rules in a table but does not claim "7 rules under `.semgrep/`" meaning "7 files." The claim was an SAST sub-agent misreading; no Bible edit is needed. The `.semgrep/orchestrator-rules.yaml` file with 7 rules matches the Bible's table row count exactly.
- **`pydantic-settings` UserWarning spam.** Every test that constructs `Settings()` without overriding `secrets_dir` emits `UserWarning: directory "/run/secrets" does not exist` (60+ per suite run). Candidate for `filterwarnings` in pyproject.toml.

## Decision

**ID4 is cleared to advance through the Build Loop** after the A1–A2 fix pass (pending commit). All SEV-2 findings are closed and exercised by regression tests. No SEV-1 findings. Five-shape parametrized redaction coverage + empirical ReDoS timing + pickle-block primitive + ValidationError scrubbing provide defense-in-depth against the realistic attacker model (env-var-controlling operator, startup-failure-log-reader, future DX misuse).

## Follow-up tracking

- SEV-4 — bandit install in dev venv (tooling hygiene)
- ~~SEV-4 — Bible §10.3 Semgrep count wording drift~~ (retracted — SAST agent misread; see Non-findings section)
- SEV-4 — pydantic-settings UserWarning filter in pyproject.toml
- SEV-4 — Rewire ID1 migration runner to read `require_local_fs` from `get_settings()` (tracked as BL3 doc follow-up, separate from this audit)

## Sign-off

- Implementation: commit `7fb5d2e` (initial) + (pending) A1/A2 fix commit
- Test suite: `tests/core/test_settings.py` — 67 tests, 100% branch coverage on `settings.py` (79 stmts, 18 branches)
- Full project suite: 179 tests passing post-fix
- Ruff clean; mypy `--strict` clean
- Spec: `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
