# lancache-orchestrator

A fully autonomous Python service that runs alongside [Lancache](https://lancache.net/) on a NAS or home server, proactively fills the cache with the games you actually own on Steam and Epic, and **validates cache state by reading the nginx cache directory from disk** rather than trusting a flat-file log. It owns its own SQLite database, APScheduler cron, per-platform authentication, and a FastAPI REST API on port 8765. State is exposed to operators via CLI, a single-file HTML status page, and a REST API.

> **Status:** Phase 2 (Construction). Milestone B in progress — see [`FEATURES.md`](FEATURES.md) for what's shipped, and [`PROJECT_BIBLE.md`](PROJECT_BIBLE.md) for the architecture and tech stack.

## Why this exists

Existing cache-prefill tools (SteamPrefill, EpicPrefill) track what they *think* they've cached in a flat file that drifts from the actual cache state. This service instead **reads the cache directory from disk** to establish ground truth, so it never reports a hit that the cache can no longer serve. See [PRODUCT_MANIFESTO.md](PRODUCT_MANIFESTO.md) for the full problem statement and MVP Cutline.

## Quickstart

Deployment instructions land in Phase 4 (`docs/INCIDENT_RESPONSE.md`, `RELEASE_NOTES.md`, and `HANDOFF.md`). Until then, the service is built and tested but not yet packaged for production install. Developers wanting to run the test suite locally:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/
```

## Configuration

All runtime configuration is read at startup by [`src/orchestrator/core/settings.py`](src/orchestrator/core/settings.py) (the ID4 settings module — see [ADR-0010](docs/ADR%20documentation/0010-settings-module-design.md)). Values resolve in this precedence order:

1. Constructor kwargs (tests only)
2. Environment variables (`ORCH_*` prefix)
3. `.env` file in the working directory (gitignored; dev convenience only)
4. Files under `/run/secrets/` (Docker secret mounts; production)
5. Built-in defaults

The bearer token (`orchestrator_token`) is the only required field. Every other field has a sensible default.

### Env var quick reference

| Env var | Type | Default | Notes |
|---|---|---|---|
| `ORCH_TOKEN` *(or secrets file `orchestrator_token`)* | string ≥32 chars | **required** | API bearer; whitespace stripped |
| `ORCH_API_HOST` | string | `127.0.0.1` | Warns if not loopback |
| `ORCH_API_PORT` | int (1..65535) | `8765` | |
| `ORCH_CORS_ORIGINS` | JSON list | `[]` | Warns on `"*"` |
| `ORCH_LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR / CRITICAL | `INFO` | |
| `ORCH_DATABASE_PATH` | path | `/var/lib/orchestrator/orchestrator.db` | |
| `ORCH_REQUIRE_LOCAL_FS` | strict / warn / off | `warn` | Refuses boot on network FS if `strict` |
| `ORCH_STEAM_SESSION_PATH` | path | `/var/lib/orchestrator/steam_session.json` | |
| `ORCH_EPIC_SESSION_PATH` | path | `/var/lib/orchestrator/epic_session.json` | |
| `ORCH_LANCACHE_NGINX_CACHE_PATH` | path | `/data/cache/cache/` | Lancache container path |
| `ORCH_CACHE_SLICE_SIZE_BYTES` | int (>0) | `10485760` (10 MiB) | |
| `ORCH_CACHE_LEVELS` | nginx levels | `2:2` | |
| `ORCH_CHUNK_CONCURRENCY` | int (1..256) | `32` | Warns if > Spike-F ceiling 32 |
| `ORCH_MANIFEST_SIZE_CAP_BYTES` | int (>0) | `134217728` (128 MiB) | |
| `ORCH_EPIC_REFRESH_BUFFER_SEC` | int (≥0) | `600` | Pre-expiry refresh window |
| `ORCH_STEAM_UPSTREAM_SILENT_DAYS` | int (≥1) | `15` | OQ4 fork-trigger threshold |

Full descriptions, validators, and design rationale: [`FEATURES.md` — Feature 3](FEATURES.md). Sensitive values (the bearer token specifically) are redacted across all serialization paths (`repr`, `model_dump`, `model_dump(mode="json")`, JSON schema) and pickling is explicitly blocked — see ADR-0010 §D4 for the three-layer redaction defense.

### Production secret handling

The bearer token should be deployed as a **Docker secret** mounted at `/run/secrets/orchestrator_token`, not as an env var. The settings module also supports `ORCH_TOKEN` as an env var for development; if both are set in production, a `config.secret_shadowed_by_env` warning is logged so you can diagnose.

## Repository layout

| Path | Contents |
|---|---|
| `src/orchestrator/` | Application source (8 subpackages: `api`, `adapters/steam`, `adapters/epic`, `core`, `db`, `validator`, `cli`, `status`) |
| `tests/` | Test suite mirroring `src/` layout |
| `docs/ADR documentation/` | Architecture decision records |
| `docs/security-audits/` | Per-feature post-audit findings + fixes |
| `docs/phase-0/`, `docs/phase-1/` | Frontloaded design artifacts (FRD, threat model, data contract, interface spec) |
| `docs/superpowers/specs/` | Feature design specs |
| `docs/superpowers/plans/` | Feature implementation plans |
| `migrations/` (legacy) | Schema migrations now live under `src/orchestrator/db/migrations/` and ship as Python package data |

## Documentation map

| What you want | Where to look |
|---|---|
| What does this thing do, and why? | [`PRODUCT_MANIFESTO.md`](PRODUCT_MANIFESTO.md), [`PROJECT_BIBLE.md`](PROJECT_BIBLE.md) §1–§3 |
| What's been built so far? | [`FEATURES.md`](FEATURES.md), [`CHANGELOG.md`](CHANGELOG.md) |
| Architecture decisions | [`docs/ADR documentation/`](docs/ADR%20documentation/) |
| Security posture and threat model | `PROJECT_BIBLE.md` §3, `docs/phase-1/threat-model.md`, `docs/security-audits/` |
| API surface (when shipped) | `docs/phase-1/interface-spec.md`, [`PROJECT_BIBLE.md`](PROJECT_BIBLE.md) §9 |
| How to contribute / agent prompts | [`CLAUDE.md`](CLAUDE.md) |

## License

Not yet declared. The project is currently a personal-deployment build; a license decision precedes any external-contribution opening (Phase 4 handoff).
