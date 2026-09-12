# UAT 15 — Automated suite agent

**Verdict: PASS. No SEV-1 or SEV-2.**

| Run | Result | Time |
|---|---|---|
| `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest` | 1863 passed, 3 deselected, 1 warning | 68.09 s |
| same with `-m slow` | 3 passed | 63.05 s |
| `ruff format --check .` | 279 files formatted, exit 0 | — |
| `ruff check .` | all checks passed | — |
| `mypy --strict src/` | no issues, 109 files | — |

1866/1866 collected and green. Only warning is third-party (Starlette httpx2 deprecation).

## Writer guard proven, not assumed
Scratch copy of `src/` + the guard test, with a planted
`UPDATE games SET status='up_to_date' WHERE id=?` in a new router module.
Guard failed as designed. No tracked file modified.

## Findings
**SEV-3 — guard regex recognises only one spelling.** `tests/test_measurement_writer_guard.py:32-46`.
10 legal SQLite variants evade it. None exist in the tree today (`UPDATE games SET` 7,
`UPDATE jobs SET` 5, `UPDATE platforms SET` 2, zero subqueries in any SET), so this is
future-regression exposure. See the exploratory agent's report for the fuller list of nine.

**SEV-3 — `VALIDATE_TIMEOUT_CEILING_SEC` not configurable.** `src/orchestrator/clients/agent_client.py`.
`chunks_per_sec` is properly plumbed through settings; the 14400 s ceiling is a bare module
constant. At 10 chunks/s ARK ModKit (359,671 chunks) computes 36,267 s, clamps to 14,400,
and times out again — the 2026-09-01 failure, unreachable by configuration. Latent at
today's 40 chunks/s (ARK = 9,292 s).

## Note
CLAUDE.md says "1835 passing". It is 1863.
