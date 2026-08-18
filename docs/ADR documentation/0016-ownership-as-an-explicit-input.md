# ADR-0016: Ownership as an Explicit Input

<!-- Last Updated: 2026-08-16 -->

**Status:** Proposed — 2026-08-16. Raised by Karl after 11 owned Steam games were found
permanently invisible to the orchestrator. Records the diagnosis and the options; the
choice in §5 is **not yet made**.

> **Update 2026-08-17 — this ADR's *invariant* stands; its *mechanism* is resolved elsewhere.**
> §3's invariant — *ownership is an explicit input, never inferred from cache
> contents* — is retained unchanged and is the durable contribution here. The
> mechanism question left open in §5 is answered by
> **[ADR-0018](0018-standalone-ownership-service.md)**.
> An intermediate attempt (ADR-0017) was **rejected by adversarial review** and has
> been removed; its errors are catalogued in ADR-0018 §1. The number 0017 is retired.
>
> Two corrections to this document, found by later verification:
> - The circular enumeration cited as `library_sync.py:89` is at **`:103`**.
> - §1.3 lists it as the *only* cache-derived enumeration site. There are **four**
>   (`library_sync.py:103`, `scheduler/jobs.py:305`, `manifest_fetcher.py:115-137`,
>   and `scheduler/jobs.py:165` which is Epic-only). Fixing one leaves new games
>   visible but inert — see ADR-0018 §1.
>
> The acute symptom was resolved operationally on 2026-08-17 (host cron
> `--recently-purchased`, plus three orchestrator defects fixed: PRs #279, #281,
> #283). All 11 games are `up_to_date`.

---

## 1. Context — the orchestrator has no Steam ownership source

`jobs/handlers/library_sync.py:89`:

```python
app_ids = [str(a) for a in await deps.agent_client.prefilled_apps()]
```

The Steam library is enumerated from apps SteamPrefill has **already prefilled** (distinct
app_ids in the manifest `.bin` cache). This is circular:

- a game enters `games` only **after** it has been prefilled;
- it is prefilled only if it is in the host's `selectedAppsToPrefill.json`;
- a newly-purchased game is in neither, so it gets no row;
- with no row, the sweep, validate and prefill pipeline can never see it.

**Ownership is inferred from cache contents.** A game the cache has never held is
indistinguishable from a game that does not exist.

### 1.1 Evidence (live, 2026-08-16)

| Observation | Value |
| --- | --- |
| Games in Game_shelf's Steam library, absent from `games` | **11** (Abyssus, Astroloot, Bloody Spell, Riftstorm, Swordcery + 6 Ghost Recon titles) |
| Rows in `games` with `status='unknown'` | **0** — they do not exist at all |
| `selectedAppsToPrefill.json` | 1144 apps, unmodified for 7 days |
| Host cron invocation (`run-steam-prefill.sh:63`) | `SteamPrefill prefill --no-ansi` — no discovery flag |
| `crontab -l \| grep -c recently-purchased` | **0** |

`prefill_cronjob.sh` *does* contain `--recently-purchased`, but nothing invokes it — dead
script. Worse, `scheduler/jobs.py:288` carries a comment asserting "the host
`--recently-purchased` cron" performs discovery. It never has. The reconcile logic is
built on a premise that was never true, which is plausibly why the gap went unnoticed.
The design spec for the prefill-ownership work (§3.2) flagged these stale comments and
they were not corrected.

### 1.2 Epic is not affected — and that is the point

`_epic_library_sync` calls `epic_client.library_enumerate()`, a real ownership API. Epic
counts agree exactly across both systems (669 rows / 669 editions). Epic obeys the
invariant; Steam does not. **The asymmetry is the defect**, introduced when re-arch ③
deleted the ValvePython Steam worker and substituted the prefilled-apps enumeration.

### 1.3 Three distinct gaps, not one

| Gap | Size | Cause |
| --- | --- | --- |
| Not in `games` at all | 11 | this ADR — ownership inferred from cache |
| In `games`, `not_downloaded`, real game | ~20 | absent from the curated selection list |
| In `games`, never type-classified | 616 | never iterated — `library_sync` only walks prefilled apps |

Only the first is addressed here. The second was handled operationally (15 app_ids added
to the selection). The third is **subsumed by this ADR**: those rows are residue from the
deleted sync and should be reconciled away once a real ownership source exists, not
back-classified.

### 1.4 What `games` currently counts

2508 Steam rows is not a game count. It is every **licensed** entry from the old sync:
684 dlc, 616 unclassified, 41 game, 11 advertising, 9 demo, 2 music. Real games ≈ 1138,
reconciling with Steam's own 1126 and Game_shelf's 1115.

---

## 2. Decision drivers

1. **Correctness** — a newly-owned game must become visible without human action.
2. **Loud failure** — a discovery source that stops working must say so. The failure that
   caused this investigation was silent: every scheduled sweep reported success while the
   library was quietly incomplete.
3. **Acyclic dependencies** — the orchestrator must remain able to function standalone.
4. **No new silent intermediaries** — every added hop is another place to go stale.
5. **Cost** — the orchestrator has no ownership integration for Amazon, Humble, itch,
   GOG, EA, Ubisoft, Xbox, Battle.net. Building eight is not proportionate.

---

## 3. The invariant

> **Ownership is an explicit input, never inferred from cache contents.**

Every option below is judged against it. This part is not controversial and should be
adopted regardless of which mechanism §5 selects.

---

## 4. Options

### A. `--recently-purchased` only (tactical)

Add the flag to the host cron. Closes the loop: new purchase → prefilled → `.bin` in cache
→ `prefilled_apps()` → `games` row → reconcile persists it into the selection.

- **For:** one line; no new credential, service, or schema; already applied to unblock today.
- **Against:** does **not** satisfy the invariant — ownership is still inferred, just with
  a faster feed. Discovery remains a side effect of prefilling. Runs outside the Piece-1
  prune, so newly-bought soundtracks and tools get cached. Gives no answer for the 616
  stale rows or for non-Steam launchers.
- **Note:** PR #275's `prefill_recent()` is the orchestrator-side equivalent, but it has
  **zero production callers** — no `AgentClient` method, no job kind, no scheduler entry.
  Merging #275 alone changes nothing here.

### B. Steam Web API `GetOwnedGames` in the orchestrator

Add a direct Steam ownership client, mirroring the Epic one.

- **For:** satisfies the invariant; restores Steam/Epic symmetry; no dependency on
  Game_shelf; failures are the orchestrator's own and can be surfaced on the `platforms`
  row exactly as Epic's `auth_status='expired'` already is.
- **Against:** needs a Steam Web API key; excludes free-to-play titles unless
  `include_played_free_games` is set (this matters — several of the 11 are F2P); respects
  profile privacy settings; does nothing for the other launchers.
- **Note:** SteamPrefill cannot substitute — `select-apps` is interactive-only and
  `select-apps status` lists only the current selection, not the owned library.

### C. Game_shelf pushes ownership

Game_shelf already runs real per-launcher API clients (13 modules;
`launchers/steam.js:31` calls `GetOwnedGames` **with** `include_played_free_games: 1`).
It already holds all 11 missing games. It is a first-class ownership source, not a cache.

- **For:** one integration covers every launcher, including the eight the orchestrator
  cannot reach; no new credentials for the orchestrator; Game_shelf is already the place
  the operator curates their library.
- **Against:** its per-launcher health is uneven — at time of writing **ea and humble
  syncs are failing, ubisoft and xbox last succeeded 2026-04-07, and amazon has no sync
  job at all** (its 509 rows come from manual downloads-folder coverage). Adopting it
  wholesale imports those failures. Requires a contract between two separately deployed
  services on two hosts.
- **Direction is load-bearing:** Game_shelf must **push**; the orchestrator must never
  call Game_shelf. A pull would make two services circular hard dependencies (LXC 1102 ↔
  1105) and would let a Game_shelf outage silently stop discovery.

### D. Hybrid — direct where available, push for the rest

Steam and Epic use direct APIs (B for Steam, existing client for Epic). Every other
launcher arrives via a Game_shelf push (C).

- **For:** each platform's ownership comes from the shortest trustworthy path; no single
  point of failure; the orchestrator keeps working standalone for the two platforms that
  matter most for prefill volume.
- **Against:** two mechanisms to maintain and reason about; the push contract still has to
  be built.

---

## 5. Recommendation (not yet decided)

**Option D**, with these conditions attached to any push-based source:

1. **Push only.** The orchestrator exposes an ingest endpoint; it never calls Game_shelf.
2. **Freshness travels with the data.** Each batch carries per-launcher
   `last_sync_at` + `status`. The orchestrator **rejects or loudly flags** a stale or
   failed launcher feed rather than silently accepting a shrunken list. Given ea/humble
   are failing and ubisoft/xbox are four months stale *today*, a naive "replace the
   library with what was pushed" would delete real coverage. This condition is the
   direct lesson of the bug this ADR documents.
3. **Ownership never deletes cache.** A game dropping out of an ownership feed marks the
   row unowned; it does not purge chunks. Purge stays operator-driven per [ADR-0015].
4. **Reconcile, then prune.** Once a trustworthy source exists, reconcile `games` against
   it and retire the 1363 licence-residue rows (§1.4). Not before.

Option A is already applied as an operational unblock and should be treated as such — a
stopgap, not the answer. It does not satisfy §3.

---

## 6. Consequences

**If D is accepted:**
- New Steam ownership client + credential handling; a push ingest endpoint + auth; a
  freshness/health schema on the ingest contract; migration to retire residue rows.
- `library_sync`'s Steam path stops being the enumeration source and becomes a cache
  reconciler. The stale comment at `scheduler/jobs.py:288` must be corrected in the same
  change.
- Game_shelf gains a push job and needs its ea/humble/ubisoft/xbox syncs repaired before
  its feed can be trusted for those launchers.

**If nothing is decided:**
- Option A keeps new Steam purchases visible, so the acute symptom stays fixed.
- The invariant stays violated: discovery remains a side effect of prefilling, the 616
  residue rows stay, and non-Steam launchers keep having no ownership source at all.

---

## 7. Open questions

- **OQ1 — Steam credential.** Is a Steam Web API key acceptable, given it requires the
  profile's game details be visible and must be stored as a secret?
- **OQ2 — F2P.** Several of the 11 are free-to-play. Confirm `include_played_free_games`
  behaviour against the account before relying on it (Game_shelf already sets it).
- **OQ3 — authority on conflict.** If a direct API and a Game_shelf push disagree about
  the same Steam title, which wins?
- **OQ4 — residue.** Delete the 1363 licence rows, or mark them `owned=0` and keep them
  for history?

---

## References

- `jobs/handlers/library_sync.py:89` — the circular enumeration
- `jobs/handlers/sweep.py:32` — candidate SQL; see the `not_downloaded` dead-end fix (PR #279)
- `scheduler/jobs.py:288` — the stale `--recently-purchased` comment
- `docs/superpowers/specs/2026-08-14-steam-prefill-ownership-design.md` §3.2 — flagged
  the stale premise; not corrected
- [ADR-0015] — purge is operator-driven and reversible
