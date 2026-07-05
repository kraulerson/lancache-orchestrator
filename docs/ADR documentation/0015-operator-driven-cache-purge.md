# ADR-0015: F18 — Operator-Driven Cache Purge

<!-- Last Updated: 2026-07-05 -->

**Status:** Accepted — 2026-07-05 (Karl approved "build it"). Supersedes the "~80 LoC"
cost estimate in issue #37; Spike G (below) is COMPLETE and its findings change the design.

**Context:**
Today's health loop — F7 disk-stat validate + F13 sweep → auto re-prefill of any
`validation_failed` game — cleanly handles the *common* failure mode: chunks the
lancache evicted are missing on disk, get flagged, and re-download. It does NOT
handle three rarer modes, because the disk-stat check only asks *"is the file
present at the right path with size > 0?"* — it never reads the bytes:

1. **Silent bit-corruption** — a chunk file exists at the correct path and size but
   its contents are damaged (bad sector, torn write). The game won't install; nothing
   flags it.
2. **Crash-orphan partial** — a prefill that died mid-write can leave a junk file
   `os.stat` reports as "present".
3. **Operator suspicion / start-over** — "this game won't install, wipe it and re-fetch."

The proposal (issue #37): a manual, operator-driven **purge** that deletes a game's
chunk files and sets `status='validation_failed'` so the existing F5/F6 path
re-prefills a fresh copy. Reactive and cheap, covering the realistic operator-reported
scenarios without a proactive full-SHA-verification scheduler.

**Key framing:** purge is **reversible**. It is not data loss — the game is always
owned on Steam/Epic and the deleted chunks re-download from the CDN. The only real
cost is transient WAN re-download. This materially lowers the risk profile.

This feature crosses the control/data-plane boundary set in [ADR-0001]: the data-plane
agent holds the lancache cache **read-only** (it validates/reads; nginx owns writes).
Purge is the first orchestrator operation that must **delete** cache files.

---

## Spike G results (the prerequisite — COMPLETE 2026-07-05)

Run live against prod (.40 lancache + agent). Three findings:

1. **✓ Cache-key formula verified against live data.** A real cached chunk file
   `…/cache/3b/3b/0c9ceb542f436559f0c55cae17283b3b` has nginx `KEY:
   steam/depot/2570211/chunk/<sha>bytes=0-10485759`, and
   `md5(KEY) == 0c9ceb…3b3b`, path `H[-2:]/H[-4:-2]/H`. The F7 cache-key formula the
   purge would reuse to compute delete targets is **exact**.

2. **⚠️ PIVOTAL: the agent's cache mount is deliberately READ-ONLY.**
   `docker inspect orchestrator-agent` → `/lancache/lancache/cache -> /data/cache
   RW=false mode=ro`. An agent-side `unlink` fails with *"Read-only file system"*.
   Only `lancache_monolithic` holds the cache `RW=true`. So purge — as issue #37
   assumed (a simple `os.unlink`) — **cannot** run on the agent (nor the control
   plane, which does not mount the cache at all — it lives on the host). Deleting a
   cache file requires **write access to the lancache cache**, which nothing in the
   orchestrator currently (by design) has.

3. **✓ Repopulation is safe.** nginx `proxy_cache` treats a missing cache file as an
   ordinary cache MISS and re-fetches from upstream on the next request — standard,
   well-established behavior — and the agent puller's re-fetch-through-lancache path
   is already exercised by every daily prefill. A deleted chunk repopulates via the
   same auto-re-prefill the purge triggers. (A direct from-host HTTP probe 508-loops
   because lancache DNS resolves Steam hosts back to itself; irrelevant to the
   feature, which repopulates via the puller, not a host-local client.)

**Consequence for cost:** issue #37's "~80 LoC" is wrong. Purge additionally requires
**relaxing the agent's read-only cache mount to read-write** (a deploy + a
defense-in-depth reduction), which is the real decision this ADR turns on.

---

## Decision

Build operator-driven, **per-game**, reversible cache purge, with the delete performed
by the **data-plane agent** (mirroring validate — the LXC brain still never touches the
cache filesystem), and **relax the agent's cache mount to read-write**, contained by
strict path validation. Rationale: the covered failure modes are real (if rare), the
operation is reversible and audited, the agent is already a trusted binary-runner, and
the path-safety guards bound the blast radius of a bug to "delete a chunk that then
re-downloads."

If the read-only boundary is judged too valuable to relax (see Alternatives), the
fallback is **Defer** — the operator can `rm` + re-prefill by hand in the rare case.

## Architecture

Mirrors the validate flow exactly (control orchestrates + updates status; agent does
the disk work):

1. **API** `POST /api/v1/games/{platform}/{app_id}/purge` (bearer-auth, idempotent) →
   enqueues a `purge` job. **CLI** `orchestrator-cli game <platform>/<app_id> purge`.
   **UI** "Delete from cache" on GameDetail behind a confirm dialog.
2. **Control handler** loads the game's manifest (same source validate uses), computes
   each chunk's cache-key hash via the F7 formula, and calls the agent.
3. **Agent** `POST /v1/{steam,epic}/purge` — receives the chunk hashes (32-hex,
   validated), computes paths under the nginx cache root, `os.unlink()`s each present
   file, returns `{files_deleted, files_failed, bytes_freed}`. **Path safety:** hashes
   match `^[0-9a-f]{32}$`; every resolved path must be a descendant of the configured
   cache root (reject otherwise) — no path can escape the cache tree.
4. **Control** sets `games.status='validation_failed'` (→ F5/F6 re-prefills next cycle)
   and writes an audit `jobs` row `kind='purge'` with the counts. `game.purged` log
   event `{game_id, files_deleted, files_failed, total_bytes_freed}`.
5. **Migration:** extend the `jobs.kind` CHECK to add `'purge'` (STRICT-table rebuild,
   as migrations 0002/0009 did for prior kinds).

**Idempotent:** a never-cached game → `200 {deleted: 0}`. **Block-list = "purge and
keep purged":** purge always sets `validation_failed`, but a block-listed game's
auto-re-prefill is suppressed, so `block` + `purge` reclaims space permanently — no
separate no-re-prefill mode needed. **Deploy:** the agent's cache mount changes
`:ro → :rw` in the agent compose/run definition (`deploy-agent.sh`).

## Consequences

- **The agent gains write/delete access to the entire lancache cache.** This removes a
  defense-in-depth property (a buggy/compromised agent can now damage the cache). Bounded
  by: the path-validation guards, the verified cache-key formula, reversibility, and the
  audit trail — but it is a real reduction and must be a conscious choice.
- Purge is bounded to per-game, whole-game, reversible deletes. No cache-wide purge, no
  chunk-level purge, no bit-integrity verification (all explicit non-goals of #37).
- A game with no fetched manifest cannot be purged (nothing to enumerate); the handler
  must surface that as a clear error, not a silent no-op. (Depot 2570211 in the spike had
  0 manifest rows — a real case: host-cron-prefilled content the orchestrator hasn't
  manifest-fetched.)
- WAN cost on re-prefill is proportional to the purged game's size.

## Alternatives considered

- **Control-plane does the `unlink` (as #37 originally wrote it).** Rejected: the LXC
  control plane does not mount the cache (it is on the host); wiring cross-host cache
  writes or SSH-exec re-crosses the ADR-0001 split the re-arch established, worse than
  the agent path.
- **Proactive full-SHA verification scheduler ("F18-original").** Rejected in #37
  itself: re-reads and re-checksums every byte of every game continuously — hours of
  disk churn for a rare problem. Purge is the cheap reactive alternative.
- **Keep the agent read-only; add a separate write-scoped "purge helper".** A minimal
  sidecar on the host with RW cache access and a single path-validated delete endpoint.
  Preserves the agent's read-only property but adds a new deployable + its own auth
  surface. Heavier than relaxing one mount flag; revisit only if the read-only boundary
  proves worth the extra component.
- **Defer / don't build.** The common failure mode (eviction) is already handled; the
  three uncovered modes are rare and the operator can `rm` + re-prefill by hand. Viable
  if the read-only relaxation is judged not worth a rare, reversible convenience.

## References

- Issue #37 (F18 proposed MVP-tier: cache purge).
- [ADR-0001] Orchestrator architecture (control/data-plane split; read-only cache).
- Spike G: this ADR's "Spike G results" section (run 2026-07-05).
- F7 cache-key formula; validate flow (`jobs/handlers/validate.py`, agent `/v1/stat`,
  `/v1/{steam,epic}/validate`).

[ADR-0001]: 0001-orchestrator-architecture.md
