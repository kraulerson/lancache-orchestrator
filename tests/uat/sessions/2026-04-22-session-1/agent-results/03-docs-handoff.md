# UAT-1 Docs & Handoff Report

## Files reviewed

- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/CHANGELOG.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/FEATURES.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/docs/ADR documentation/0008-migration-runner-architecture.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/docs/ADR documentation/0009-logging-framework-architecture.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/docs/security-audits/id1-sqlite-migrations-security-audit.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/docs/security-audits/id3-structured-logging-security-audit.md`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/CLAUDE.md` (consistency only)
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/PROJECT_BIBLE.md` (§5 + Last Updated markers)
- Verified against source: `src/orchestrator/db/migrate.py`, `src/orchestrator/core/logging.py`, `src/orchestrator/db/migrations/{0001_initial.sql,CHECKSUMS}`, `tests/db/test_migrate.py`, `tests/core/test_logging.py`, `gh issue list`, `git cat-file` for hashes.

## Accuracy findings

- **MATCH** — ADR-0008 D1 "single BEGIN IMMEDIATE wraps read+apply": `migrate.py:415` opens `BEGIN IMMEDIATE`, the read (`applied_rows`, line 417) and the apply loop (lines 442–452) are both inside the same try, `COMMIT` at 459, `ROLLBACK` under `suppress(sqlite3.Error)` at 461–462. Exactly as claimed.
- **MATCH** — ADR-0008 D2 "CHECKSUMS manifest, SHA-256 pinned, loaded pre-transaction": `_verify_checksum_manifest` called at `migrate.py:394`, before `sqlite3.connect` at 398. `src/orchestrator/db/migrations/CHECKSUMS` exists with the documented `<id> <sha256> <filename>` format and a pinned entry for `0001_initial.sql`.
- **MATCH** — ADR-0008 D3 "importlib.resources package-resource loader": `import importlib.resources as resources` at `migrate.py:10`; `_package_migrations_root()` used at 389; top-level `migrations/` directory is gone; `src/orchestrator/db/migrations/__init__.py` present.
- **MATCH** — ADR-0009 D1 "token-based reset": `structlog.contextvars.bind_contextvars` at `logging.py:118`, `reset_contextvars(**tokens)` in `finally` at 122. `test_nested_request_context_restores_outer_cid` exists at `tests/core/test_logging.py:319`.
- **MATCH** — ADR-0009 D2 "_protect_reserved_keys with numbered slot": processor defined at `logging.py:130`, `RESERVED_KEYS` frozenset at 34 contains the documented 6 keys, loop at 150. `test_protect_reserved_keys_collision_uses_numbered_slot` at `tests/core/test_logging.py:345`.
- **MATCH** — ADR-0009 D3 "letter-class boundaries, cycle-safe": `_SENSITIVE_KEY_RE` at `logging.py:55`, `_walk` seen-set of `frozenset[int]` at 176, `test_cyclic_event_dict_does_not_recurse_infinitely` at line 428.
- **MATCH** — ADR-0009 D4 "strict log_level validation raises ValueError": `_VALID_LOG_LEVELS` at `logging.py:38`; `configure_logging` raises `ValueError` at 208–210; `pytest.raises(ValueError)` hit at `tests/core/test_logging.py:305`. FEATURES claim verified against test.
- **MATCH** — Test counts. FEATURES / audits claim "42 tests in tests/db/test_migrate.py", "55 tests in tests/core/test_logging.py". Live pytest `--collect-only`: 42 and 55 exactly (plain `def test_` undercounts — parametrize expansions bring it up).
- **MATCH** — Commit hashes. `3f4ea85`, `8ca6658`, `15203c6`, `13e0843` all resolve via `git cat-file -e`. (`46f2fd2` and `deec8c9` referenced in ADR "Supersedes" lines were not in local log but are pre-rewrite; not asserted against.)
- **MATCH** — All GH-issue references in CHANGELOG (#3–#15) are closed; follow-up refs (#19–#22) are open. `gh issue list` confirms. Directionality is correct: Security/Fixed cite closed CVE-like issues; FEATURES "Known Limitations" + CHANGELOG "Removed #7" reference the right issues.
- **MISMATCH (minor, cosmetic)** — ID1 audit cites specific line numbers in `migrate.py` that no longer match the current file. Audit table says `BEGIN IMMEDIATE` at line 374; actual line 415. `_assert_local_filesystem` at 345; actual 386. Post-apply / ROLLBACK lines (413/415–417) are off by ~40. Function identities and fix semantics are correct — only the line addresses drift because the file grew during the N1–N4 / F1–F6 hardening passes. A future maintainer can still grep the function names.
- **MATCH** — ID1 audit `_CREATE_TABLE_RE` location (`migrate.py:55-58, 286-295`), `_verify_checksum_manifest` (`migrate.py:212-241`) — close enough to current (`_verify_checksum_manifest` now at 253); same drift pattern as above.
- **MATCH** — CHANGELOG Infrastructure entry states Dockerfile no longer `COPY`s `migrations/`. Confirmed: top-level `migrations/` directory does not exist.

## Internal consistency findings

- **Consistent** — ID1 audit table #7 row marks the rollback-runner issue "CLOSED"; CHANGELOG "Removed" section cites #7; ADR-0008 "No rollback runner" note matches; FEATURES Known Limitations carries forward.
- **Consistent** — Re-audit labels align across all three ID3 docs: BL2 N1/N2/N3/N4 fixed in `13e0843`, N5 deferred to #22. BL1 F1/F2/F6 fixed in `8ca6658`, F3/F4/F5 tracked as #19/#20/#21.
- **Consistent** — Test-count claims agree across audit (42 / 55 ; "112 project-wide" for ID3 audit matches ID1's "57 project-wide" + 55, approximately; ID3 audit line 20 says "52 regression + baseline tests" in Scope but 55 in Sign-off — mild internal inconsistency inside the ID3 audit, worth noting).
- **Minor inconsistency** — ID3 audit `Scope` header says `tests/core/test_logging.py (52 regression + baseline tests)` but Sign-off says `(55 tests; 112 project-wide)`. Actual pytest collection = 55. The "52" in the Scope section is stale from a pre-final draft.

## Staleness in PROJECT_BIBLE / CLAUDE.md

- **PROJECT_BIBLE §5 header** — `<!-- Last Updated: 2026-04-20 -->`. Predates both BL1 and BL2. Not refreshed after ID1 / ID3 ship.
- **PROJECT_BIBLE §5 line 217** — points at `migrations/0001_initial.sql`. Canonical location is now `src/orchestrator/db/migrations/0001_initial.sql` (per ADR-0008 D3). Stale path reference, no impact on behavior.
- **PROJECT_BIBLE §5.4 line 253** — describes migrations living in `migrations/` and references rollback-scripts as "optional `NNNN_..._down.sql`". Both are now wrong: location changed and rollback was intentionally removed (#7, CHANGELOG "Removed"). Says "Runner (~50 LoC)" — current runner is 483 LoC.
- **PROJECT_BIBLE §5.4** — references `migration_content_drift` CRITICAL and `schema_version_ahead` event codes; neither appears in `migrate.py` (`grep` shows only `migration_applying`/`migration_applied`/`migrations_complete`). The documented critical-path telemetry names do not exist in code.
- **PROJECT_BIBLE §16 line 711** — `migrations/ — 1–2 SQL files at MVP` path reference is stale.
- **CLAUDE.md** — consistency only per scope; no issues to flag.

## Verdict

Ship-ready for UAT with one cosmetic note: the ID3 audit's Scope header says "52 regression + baseline tests" where Sign-off and reality say 55. Worth a one-character fix but not a blocker for the tester. The ID1 audit line-number drift (374→415, 345→386, etc.) is cosmetic; the fix semantics and function identities remain correct and greppable.

PROJECT_BIBLE §5 / §16 staleness (path `migrations/` vs `src/orchestrator/db/migrations/`, rollback references, `~50 LoC` claim, non-existent `migration_content_drift` event name) is real but explicitly out of scope of this review — flagging per the prompt, not proposing a fix. A future maintainer reading only the Bible would not find the current migration files at the documented path, though they'd quickly find them via grep.

No doc correction required before sending the UAT template to the tester.
