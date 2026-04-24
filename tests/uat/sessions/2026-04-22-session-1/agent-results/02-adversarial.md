# UAT-1 Adversarial Report

Persona: Malicious User. Read-only review of cross-feature interactions (ID1 migrations × ID3 logging) and new infra surfaces (Dockerfile digest pin + CI arm64 QEMU build). Previously-audited material (per-feature BL re-audits, GH #3–#22) is explicitly out of scope.

## Files reviewed

- `src/orchestrator/db/migrate.py` (log call sites, CLI entrypoint, package-data manifest loading)
- `src/orchestrator/core/logging.py` (redactor regex, reserved-key protection, request_context)
- `src/orchestrator/db/migrations/0001_initial.sql` and `src/orchestrator/db/migrations/CHECKSUMS`
- `Dockerfile` (digest pin, USER, COPY ownership, HEALTHCHECK)
- `.github/workflows/ci.yml` (build job: QEMU arm64, GHA cache, needs graph)
- `.github/` (absent: `dependabot.yml`, `renovate.json`)
- Runtime behavior probed by instantiating `configure_logging()` and `structlog.get_logger()` with and without `request_context()`

## Findings — cross-feature interactions

### [SEV-3] Migration startup logs are emitted without any `correlation_id`, creating an audit-trail gap
- **Trigger:** `run_migrations()` is called at boot before any API request handler / job runner wraps the call in `request_context()`. The four `log.info` calls in migrate.py (`migration_applying`, `migration_applied`, `migrations_complete`, `filesystem_type_unknown`) and the `log.critical("migrations_failed", ...)` therefore run with an empty contextvars map.
- **Impact:** An operator running a SIEM/audit-log query "show me everything correlated with correlation_id=X during incident Y" cannot link the migration event stream to any request. More concretely: if an attacker triggers a corruption-leading migration-failure path (e.g., a poisoned wheel — see next finding) and that failure occurs mid-rollout during a user-initiated upgrade API call, there is no way post-hoc to connect the two log streams. Verified empirically: the first log line output by `log.info("migrations_complete", ...)` outside `request_context()` emits JSON with no `correlation_id` field at all — it is silently absent, not e.g. `"correlation_id": "startup"`.
- **Evidence:** `src/orchestrator/db/migrate.py:445,452,466,472,477`; `src/orchestrator/core/logging.py:100–122` (only path that binds cid is `request_context()`); confirmed via runtime smoke: the field simply does not appear.
- **Fix hint:** In `_cli()` and at any Python-level call into `run_migrations`, wrap with `request_context(correlation_id="startup-<git_sha>")` or bind a synthetic `"startup"` value so migration lines are attributable. Alternatively, document explicitly that missing cid ⇒ startup phase and have the log pipeline backfill `"correlation_id": "boot"` when absent. This is a traceability gap, not an integrity bug, hence SEV-3.

### [SEV-3] Redactor is key-only; future migration *names* containing sensitive substrings will leak via value channel — but the inverse false-positive is not possible for current fields
- **Trigger:** The redactor walks dict keys only (`logging.py:183`). Values are never inspected. Migration `name` values are freely chosen (`_MIGRATION_NAME_RE` allows `[a-z0-9_]+`). A future migration named e.g. `0005_add_sessions_table.sql` will be logged as `{"name": "add_sessions_table"}` — `name` as a key does not match, so the value passes through. That's correct behavior (the name is not a secret), but the design hides an asymmetric gap: if migration file content ever legitimately contains credential-looking data (e.g., a seed row for a default service account with a default token — not current, but plausible in later MVP features), an error path that logs SQL excerpts will bypass redaction.
- **Impact:** Current migrate.py logs only `migration_id`, `name`, `db_path`, `hint`, `applied_count`, `error`, `argv` — I verified none of these keys match the redactor regex, so redaction will not wrongly fire on migration output. But it also means `error=str(e)` (`migrate.py:477`) can contain arbitrary SQLite error text including reflected literal values. If a future migration seeds any credential-adjacent column, a `NOT NULL` or `UNIQUE` violation produces an error message containing that literal, and it is logged unredacted.
- **Evidence:** `src/orchestrator/db/migrate.py:477` (`log.critical("migrations_failed", error=str(e))`); `src/orchestrator/core/logging.py:176–188` (walk only rewrites dict keys). SQLite's `IntegrityError` message format reflects conflicting column values on some constraint types.
- **Fix hint:** Add a coding-guideline check (semgrep rule in `.semgrep/`) that forbids `log.*(error=str(e))` and requires structured fields instead, OR: in `migrate.py` the CLI, strip the exception message to the first 120 chars and strip anything matching a `'...'` literal before logging. Tracked separately from #20 / #21 because those are file-format footguns; this is a log-channel footgun.

### [SEV-4] `argv` kwarg in `_cli()` leaks the entire process argv to logs — includes any accidentally-passed env-style params
- **Trigger:** `src/orchestrator/db/migrate.py:472` — `log.error("migrate_cli_usage", argv=sys.argv)` on wrong-usage. The key `argv` does not match the redactor. An operator who runs `python -m orchestrator.db.migrate --token=xyz` (wrong but plausible mistake — the CLI rejects unknown args but the argv is already logged) will have the token captured.
- **Impact:** Low — requires operator error combined with credential-in-argv, and the CLI only runs interactively during bootstrap. But it's a classic credential-leak footgun because argv is world-readable on many systems (`/proc/self/cmdline`).
- **Evidence:** `src/orchestrator/db/migrate.py:471–472`.
- **Fix hint:** Log `argc=len(sys.argv)` and `script=sys.argv[0]` only, not the full vector.

### No-finding: migrate output does not over-redact
I confirmed by direct regex eval that none of the keys migrate.py uses (`migration_id`, `name`, `db_path`, `hint`, `applied_count`, `error`, `argv`, `event`, `level`, `timestamp`) match `_SENSITIVE_KEY_RE`. No false-positive redaction risk for migration events today. (Short tokens `pin/otp/mfa/tfa/sid/creds/salt/nonce` have letter-class boundaries — e.g., `migration_id` does NOT match `sid` because of the leading `n_`, and `name` does not match anything.)

### No-finding: writable wheel attack requires prior root compromise
Re the CHECKSUMS-+-SQL-+-regex triple-rewrite attack: in the shipped image, `/app/src/**` is owned by root (Dockerfile `COPY --from=builder` creates root-owned paths) and the container's runtime USER is `orchestrator` (uid 1000) — so a post-exploit RCE at uid 1000 cannot modify the wheel. The attack requires either (a) prior privilege escalation to root inside the container, or (b) a poisoned image from the registry pipeline. Both are out of the migrations-module threat model and belong to supply-chain / container-hardening. Noting it only to record that last-line-of-defense = root ownership of `/app/src`, NOT Docker's read-only root filesystem (see infra finding below).

## Findings — infra hardening surfaces

### [SEV-3] Digest-pinned base image has no update policy — no Dependabot, no Renovate
- **Trigger:** `Dockerfile:2,21` pin `python:3.12-slim@sha256:520153e2…` in both stages. `.github/` contains only `workflows/` — no `dependabot.yml`, no `renovate.json`. The digest will therefore drift from upstream forever unless a human manually bumps it.
- **Impact:** Any CVE disclosed in the underlying Debian bookworm slim layer (glibc, zlib, openssl, Python itself) does not propagate into the pinned image. The project will be running known-vulnerable base packages N days after disclosure, where N = latency between release tag and when a human notices. For a LAN-only service this is lower-severity, but `pip-audit` covers only Python deps — it cannot see OS-layer CVEs.
- **Evidence:** `Dockerfile:2,21`; `ls .github/` returns only `workflows/`.
- **Fix hint:** Add `.github/dependabot.yml` with a `docker` ecosystem entry scheduled weekly, OR `renovate.json` with `pinDigests:true` and a schedule. Combined with CI arm64 rebuild, this gives safe automated digest bumps.

### [SEV-4] QEMU-emulated arm64 build masks ISA-specific WAL behavior
- **Trigger:** `ci.yml:162–205` builds linux/arm64 under QEMU (`docker/setup-qemu-action@v3`). QEMU's user-mode emulation translates syscalls and emulates atomics via host primitives — it does NOT exercise native arm64 mmap alignment semantics, acquire/release memory-ordering on `ldxr/stxr`, or Apple-silicon-style 16K page sizes.
- **Impact:** SQLite's WAL mode uses `mmap_size = 268435456` (`migrate.py:405`). On native arm64 hosts with 16K pages (Apple M-series) or weakly-ordered cores, there have historically been SQLite mmap corruption issues. QEMU doesn't model this. The CI guarantees only "arm64 wheels install cleanly and the Python entry points import" — not "WAL works correctly under contention on a native arm64 kernel." The DXP4800 (primary prod target) is aarch64 Linux with 4K pages so lower risk, but the false sense of coverage is real.
- **Evidence:** `ci.yml:192–205` comment literally says "verify-only"; no integration tests run inside the arm64 image.
- **Fix hint:** Either (a) document explicitly in `docs/ADR documentation/` that arm64 coverage is compile/import-only and integration testing requires a native aarch64 runner pre-release, or (b) add a tag-triggered job that runs `pytest tests/db/test_migrate.py` inside the built arm64 image under QEMU (slow but catches obvious WAL breakage).

### [SEV-3] GHA cache-from/cache-to on public fork PRs can be poisoned by a contributor
- **Trigger:** `ci.yml:179–180,204–205` use `cache-from: type=gha / cache-to: type=gha,mode=max` in the `build` job, which runs on `pull_request`. By default, GitHub Actions cache is scoped per-ref: reads fall through to the base branch's cache, writes go to the PR's ref-scoped cache. A PR from a fork cannot write to main's cache (GHA enforces this). BUT the `build` job runs without a `paths:` filter, and `mode=max` exports all intermediate layers — a malicious PR can:
  1. Submit a PR that modifies the Dockerfile to add a poisoned layer early, causing `cache-to` to store the poisoned layer under the PR's scope.
  2. Because the base image digest and builder layer hash are identical, a subsequent innocuous PR from the same fork sees its own branch-scoped cache hit, reusing the poisoned layer.
- **Impact:** This is a self-contained fork-scoped poisoning, not a cross-PR contamination (GHA does correctly isolate by ref). But it enables a malicious PR to silently smuggle code into its own image build that then passes SAST (Semgrep scans source, not built image). If that image is ever tested locally by a maintainer via `docker pull`, they execute attacker code.
- **Evidence:** `ci.yml:179–180`; fork PRs trigger `build` with `needs: [lint, test, sast, secrets, deps, licenses]` — all of which scan source, none scan the built image. The `verify image size` step is the only action performed ON the built artifact.
- **Fix hint:** Either (a) skip `cache-to` on PRs from forks (`if: github.event.pull_request.head.repo.full_name == github.repository`), (b) add `trivy image` or `grype` scanning of the built image in the `build` job so a poisoned layer surfaces, or (c) use `cache-from: type=gha, scope=main` read-only on fork PRs so they can't poison their own cache either. Given repo is public and accepts fork PRs, option (b) is the most defense-in-depth.

### [SEV-4] Runtime container does not set read-only rootfs despite the threat model requiring it
- **Trigger:** Dockerfile sets `USER orchestrator` but does NOT declare `VOLUME` for the state dir or use `--read-only` semantics. The threat model (`docs/phase-1/threat-model.md:254`, PROJECT_BIBLE.md `read_only: true`) assumes compose-level `read_only: true` hardens the runtime. That's a compose-file property, not a Dockerfile property, so technically not a Dockerfile bug — but the pinned Dockerfile is the unit of "infrastructure hardening" just shipped, and without a compose example committed alongside, operators running `docker run ghcr.io/...:latest` get a writable rootfs by default. The wheel-tamper defense discussed above depends on both file ownership AND read-only root to be robust; today only the former holds.
- **Impact:** Defense-in-depth gap. A root-equivalent RCE (CVE in uvicorn / Python / stdlib) would be able to rewrite `/app/src/orchestrator/db/migrations/*` in-memory for the life of the container and persist there (overlay FS, not state volume). Until a compose bundle with `read_only: true` ships, the hardening promised by the threat model is incomplete.
- **Evidence:** `Dockerfile` full file — no `VOLUME`, no `STOPSIGNAL`, no enforcement mechanism. Present only in external docs.
- **Fix hint:** Either add `VOLUME ["/var/lib/orchestrator"]` and document that `--read-only` is mandatory, or commit a `docker-compose.yml` with `read_only: true` at the project root so the hardening is an enforced artifact not a planning document.

## Non-findings

- **Redactor false positives on migrate output**: verified — none of migrate.py's current kwargs trigger `_SENSITIVE_KEY_RE`.
- **Reserved-key clash with migrate kwargs**: migrate.py does not use `event`, `level`, `timestamp`, `logger`, `logger_name` as kwargs — no clash path.
- **request_context leakage across migrations**: migrate runs synchronously, one-shot at boot, no threading — no chance of cid bleed across requests.
- **Checksum manifest supply-chain on disk**: covered by existing #21 (filename validation) and root-ownership in image.
- **`_split_sql` with migration SQL literals**: covered by #19.
- **Statement splitter and `CREATE TEMP`/virtual tables**: covered by #20.
- **`_walk` perf**: covered by #22.
- **Migration runner atomicity**: covered by BL1.
- **`pip-audit` vs. `snyk` swap**: audited at infra-change time, not re-opened here.

## Overall adversarial posture

The ID1 × ID3 union is safe enough to hand to a human UAT tester — there are no cross-feature integrity or confidentiality bugs that would cause a tester to hit corruption or credential leak. The findings above are hardening / observability gaps (correlation-id on startup, argv echo, image update policy, fork-PR cache, read-only rootfs), not interaction bugs. I would prioritize the Dependabot/Renovate add (SEV-3) before Milestone C ships, because once arm64 multi-arch images start being published on tags, a stale base image becomes a prod exposure, not just a hypothetical.
