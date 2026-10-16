# UAT Session 18 — Automated suite

**Run by:** Claude (controller), 2026-10-16 ~14:50 UTC. Karl asked for UAT 18 to be performed by Claude.
**Tree:** `main` @ `c7a3ee2` (merge of PR #370; contains PR #368 / #361).
**Features in this batch:** Feature 30 one-login Steam manifest helper (#361, PR #368); `fetch-manifests-ratio-7pct` (PR #370).

| Check | Command | Result |
|---|---|---|
| Python suite | `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider` | `2064 passed, 3 deselected, 1 warning in 81.07s`. The warning is pre-existing: starlette's `httpx` deprecation in `fastapi/testclient.py`, a dependency. |
| Helper suite (.NET 10.0.105) | `cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests` | `Passed!  - Failed: 0, Passed: 60, Skipped: 0, Total: 60` |
| Lint | `ruff check src tests` / `ruff format --check src tests` | `All checks passed!` / `297 files already formatted` |
| Types | `mypy --strict src` | `Success: no issues found in 111 source files` |
| SAST, project rules (Python) | `semgrep --config .semgrep src tools --quiet` | no output, rc=0 |
| SAST, C# | `semgrep --config p/csharp tools/steam_manifest_helper` | `Ran 27 rules on 19 files: 1 finding`, which is `unsafe-path-combine` at `SessionStore.cs`. This is the same finding the #361 security audit (check 6) adjudicated as a false positive: `sessionDir` comes from the agent's own settings or the operator's argv. This run covers all 19 C# files, including the fix wave's `ConnectRetry.cs`, closing the scan-scope residual. |
| Secrets | `gitleaks detect --log-opts="312e16f..HEAD"` | `no leaks found` |
| CI on `main` | GitHub Actions | PR #368 merge: 8/8 checks, `Image size: 254 MB` (limit 275). PR #370 merge: 8/8 checks. Dependabot workflow runs 2026-10-12: success. |
| Cross-platform | CI `Build` job | Builds amd64 (size-gated) and arm64 (cross-published via `$BUILDPLATFORM`, never QEMU). Both green on #368 and #370. The helper's linux-arm64 publish was also proven from a clean clone in the #361 final review. |

**Verdict:** no automated-suite failures.
