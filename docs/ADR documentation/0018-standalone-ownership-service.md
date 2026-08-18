# ADR-0018: Standalone Ownership Service — Contract First, Service on Trigger

<!-- Last Updated: 2026-08-17 -->

**Status:** Proposed — 2026-08-17. Replaces [ADR-0017], which was **rejected by
adversarial review** for the factual errors catalogued in §1; this ADR re-derives the
decision from verified code. It retains [ADR-0016]'s invariant — *ownership is an
explicit input, never inferred from cache contents* — and resolves ADR-0016 §5's open
recommendation. Spans `lancache_orchestrator` and `Game_shelf`; a third deployable is
specified (§5) but its construction is gated (§4).

**Decision in one line:** adopt the ownership *contract* now (full-snapshot +
generation, hosted by Game_shelf, pushed to the orchestrator's proven reconcile
pattern), do the credential-store hardening in place because it is prerequisite work
under every topology, and build the standalone service **only when a named trigger
fires** — with its full specification recorded here so that, if built, it is built
right the first time.

Every file:line citation below was verified on 2026-08-17 against
`lancache_orchestrator` main @ 977b262 and `Game_shelf` @ 03cd89f (HEAD of
`fix/launcher-partial-library-data-loss`; an uncommitted fix for the §3.4
partial-sync bug is in flight on that branch — citations refer to HEAD). Line numbers
drift; treat the cited *code*, not the number, as the reference.

---

## 1. Corrections register — why ADR-0017 was rejected

ADR-0017's design rested on claims that are false in the code. They are recorded here
because each one changes the design, and because this ADR must not be trusted on the
same points without re-verification.

| # | ADR-0017 claim | Verified reality |
| --- | --- | --- |
| C1 | "Battle.net stores `totp_secret`, so 2FA is generated, not prompted" — headless sync throughout | Battle.net is a **stub**: `battlenet.js:21-24` returns `[]` with a warning; registered `implemented: false` (`routes/launchers.js:19`). It stores nothing. `generateTOTPCode`/`generateSteamCode` (`utils/totp.js:8,46`) have **zero production callers** — `setup.js:3` imports only `generateQRSetupData`. The launcher that actually holds a password is **Ubisoft**: HTTP Basic login at `ubisoft.js:164`, and `refreshIfNeeded` re-persists the **plaintext password inside the credential blob on every sync** (`ubisoft.js:229-237`). |
| C2 | Headless capability implied for all launchers ("`syncAll(db)` runs on a scheduler") | Ubisoft's 2FA is an **emailed code** (`OTP_REQUIRED`, `ubisoft.js:172-182`) relayed through a two-phase job (`awaiting_otp`) with a **5-minute window** (`routes/sync.js:5,57-65`). It cannot run unattended. See §3's launcher table: ~5 of 10 registered launchers need a human to (re)auth. |
| C3 | "Launcher → DB coupling: none — `grep this.db` is 0" | The grep is misleading. `epicCatalog.js` takes a raw `db` handle as a **positional parameter** (`nestDLC(db, launcherId)` :15; `resolveCodenames(db, launcherId, session)` :67) and writes Game_shelf's `game_editions` and `games` tables directly (:34-35, :54-58, :79-84, :130-131), invoked from `syncEngine.js:145-159`, and additionally needs a **live Epic access token**. This is the single hardest extraction blocker. |
| C4 | "Output is already a neutral DTO — `{launcher_game_id, title, playtime_minutes}`" | The return shape carries ~7 fields bound **1:1 to `game_editions` column names** (`epic_namespace`, `epic_catalog_id`, `sandbox_type`, `gog_slug`, `playtime_minutes`, …) with **per-field merge semantics** in the upsert (`syncEngine.js:57-69`): `gog_slug` is COALESCEd (:67) while the Epic fields overwrite with NULL (:64-66). It is Game_shelf's schema wearing a DTO costume. |
| C5 | `since=` cursor for incremental pulls | There is **no `updated_at` column** on `game_editions` to build a cursor from, and a cursor cannot express "no longer owned" — revocation today is a soft flag flipped by full-set comparison (`syncEngine.js:113-133`). The cursor design was unimplementable as written. |
| C6 | "Neither app depends on the other; a shared service decouples them" | They **already integrate bidirectionally in production with graceful degradation**: push via `crossLauncherExclusions.js:45-59` on the daily cron (`server.js:102-109`) into the orchestrator's transactional reconcile endpoint (`api/routers/prefill_exclusions.py:131-199`); pull via `orchestrator.js:47-68` and `manualCoverage.js:200-216`; soft-503 + last-known-good already implemented (`orchestrator.js:6-43`, `manualCoverageSnapshot.js:37-44`). "Decoupling" is not a benefit on offer. |
| C7 | "Credentials at rest … fails closed" presented as adequate | `encrypt.js` is AES-256-GCM but derives the key as **unsalted single-pass SHA-256(passphrase)** with only a length ≥ 32 check (:15-23); there is **no key-rotation routine** (changing the key bricks every credential) and **no AAD binding**. Separately, `GET /api/setup/qr/:launcher_id` returns the **decrypted plaintext `totp_secret`** to any authenticated session (`setup.js:41-59`). |
| C8 | 2508 vs 1115 shows "two classifiers, two answers" — divergence as motivation | The 2508 is **licence residue** (dlc/demos/tools/unclassified) from the deleted sync worker (ADR-0016 §1.4). Real game counts agree within ~2%: ≈1138 (orchestrator) / 1126 (Steam) / 1115 (Game_shelf). The systems do **not** meaningfully disagree; disagreement must not be used to justify anything. |
| C9 | `library_sync.py:89` as the circular enumeration | The enumeration is at `jobs/handlers/library_sync.py:103`. Both prior ADRs cite the wrong line. More importantly, it is only **one of four** cache-derived enumeration sites (§2.2). |

---

## 2. Context — the problem actually on the table

### 2.1 The motivating bug (ADR-0016, restated)

The orchestrator inferred Steam ownership from cache contents: a newly purchased game
had no `games` row and was invisible to sweep/validate/prefill forever. The invariant
fix — ownership as an explicit input — is adopted and not in question here. The
question is the *mechanism*: incremental (ADR-0016 Options B–D) versus a standalone
ownership service (ADR-0017's proposal, re-examined here).

### 2.2 Feeding ownership in fixes one of FOUR enumeration sites

Any plan that treats "point `library_sync` at an ownership feed" as the fix does not
solve the motivating bug. The orchestrator derives state from cache contents in four
places:

| Site | What it does | Effect if unfixed |
| --- | --- | --- |
| `jobs/handlers/library_sync.py:103` | Populates `games` from `agent_client.prefilled_apps()` | The one an ownership feed fixes |
| `platform/steam/manifest_fetcher.py:115-137` | Agent self-enumerates fetch targets from `selectedAppsToPrefill.json` + `.bin`/`.shas` delta | An owned-but-never-prefilled game gets **no manifests**, so validate has nothing to stat |
| `scheduler/jobs.py:305-307` | Auto-classify/prune block reads `prefilled_apps()` to build the selection restore set | New games don't enter the host cron's selection except via the `--recently-purchased` stopgap |
| `scheduler/jobs.py:158-175` | Scheduled prefill enqueues **Epic only** (Piece 2: Steam prefill is host-cron-owned, by design) | Ownership ingest alone never causes a Steam byte to download |

An ownership feed without the other three yields **visible-but-inert rows**: a `games`
row exists; nothing fetches its manifests, nothing prefills it, validation has no
input. The consumer-side plumbing is the larger share of the work and is required
under every topology, service or not.

### 2.3 The consumer side has no revocation or freshness today

`owned = 0` has **no writer anywhere in orchestrator `src/`** (the schema has the
column, `0001_initial.sql:38`, default 1). `platforms.last_sync_at` is **never
written** — every reference is a read (`api/routers/platforms.py:121,152`,
`cli/commands/auth.py:45`, `api/routers/status.py:264`). And `games.platform` is
CHECK-constrained to `('steam','epic')` via the `platforms` FK
(`0001_initial.sql:15-16,35`). Whatever produces ownership, the orchestrator currently
cannot *consume* revocation, cannot *record* freshness, and cannot *hold* a non-Steam,
non-Epic row. That work exists in every option below.

---

## 3. Scope honesty — what an ownership service CAN and CANNOT deliver

The original ambition — "a source of truth that authenticates to all game launchers"
and syncs on a schedule — is **not achievable as stated**. The launcher population,
verified per adapter:

| Launcher | Library source | Steady-state unattended? | Human needed to (re)auth |
| --- | --- | --- | --- |
| steam | Web API `GetOwnedGames` (+`include_played_free_games:1`), `steam.js:28-39` | Yes | No — durable API key |
| xbox | API key (`xbox.js:10,27-30`) | Yes | No |
| itchio | API key (`itchio.js:11,26,35`) | Yes | No |
| epic | OAuth; **single-use rotating** refresh token (`epic.js:96-103`) | Yes, until the refresh chain breaks | Paste fresh auth code |
| gog | OAuth; **single-use rotating** refresh token (`gog.js:64-88`) | Yes, until refresh expiry | Paste fresh auth code |
| ea | Implicit-flow access token, **1 h expiry, no refresh** (`ea.js:71-99`) | **No** | Paste a new token, hourly |
| humble | Pasted browser session cookie (`humble.js:16-24`) | While the cookie lives | Paste fresh cookie |
| ubisoft | GraphQL (~16 native titles) + **operator file upload** for the full library (`ubisoft.js:41-160`) | Partial (`rememberMeTicket`) | **Emailed OTP** in a 5-min window; file upload |
| amazon | **File import only** — `fetchOwnedGames` throws (`amazon.js:32-34`) | **No** — there is no API | File upload (`routes/launchers.js:48,67`) |
| battlenet | **Unimplemented stub** (`battlenet.js:21-24`) | n/a | n/a |

**CAN deliver:** one place that answers "what does this account own, per launcher,
how fresh, and is it healthy"; one credential store with real key management (an
improvement over today's, §1 C7); one implementation of each launcher's quirks; sync
failure made observable; revocation semantics consumers can act on.

**CANNOT deliver:**

1. **Unattended everything.** Five of ten launchers structurally require a human
   (paste, emailed OTP, or file upload). A service can *host* the attended workflows —
   it cannot remove them. "One service syncs everything on a schedule" is the wrong
   frame; the right frame is *a credential vault plus scheduler for the unattended
   half, and an attended-workflow console for the rest* (§5.4).
2. **Prefill credential consolidation.** Discovery credentials ≠ prefill credentials.
   Steam prefill needs only `int(app_id)` (`jobs/handlers/prefill.py:324`) —
   SteamPrefill authenticates with its **own** login (`Config/account.config`,
   `core/settings.py:90`). Epic prefill needs the **orchestrator's own** Epic
   credential to fetch a fresh signed manifest every run — stored URLs expire
   (`prefill.py:131-133`). An ownership service can never remove SteamPrefill's Steam
   login or the orchestrator's Epic credential. Net secret count: Game_shelf's
   launcher credentials move; the two prefill credentials stay where they are.
3. **The motivating bug, by itself.** §2.2: one of four sites. The orchestrator-side
   work dominates and is topology-independent.
4. **Better data.** Same upstream APIs, same failure classes. The **live partial-sync
   bug** — `epic.js:126-146` and `humble.js:70-72` catch errors *inside* pagination
   and return partial lists without throwing, after which `syncEngine.js:113-133`
   marks every non-returned edition `owned=0` (guarded only against a fully-empty
   result, :116) — silently un-owns ~80% of a library on a partial Epic sync while
   reporting success. That must be fixed **wherever the adapters live**; moving them
   moves the bug.
5. **Ownership for launchers no consumer can use.** The orchestrator can only prefill
   Steam and Epic; Amazon/Humble/itch presence is already handled by the
   manual-coverage flows (`manualCoverage.js`, orch `/api/v1/manual-downloads/*`).
   Ingesting eight launchers' ownership into a consumer with nothing to do with it is
   inventory, not capability.

---

## 4. Decision

Adopt the **incremental, contract-first path** now; **gate the standalone service** on
explicit triggers. Concretely:

1. **Fix the partial-sync bug in place** (adapters throw on partial results; a failed
   page fails the sync). Prerequisite for trusting *any* ownership source.
2. **Harden the credential store in place** (§5.5–5.6: scrypt/raw-key KDF, AAD,
   key-id envelope, tested rotation routine; retire the plaintext-`totp_secret` QR
   endpoint). This work is identical whether or not a service is ever built, and it
   must exist **before** any credential migrates anywhere.
3. **Define the ownership contract** (§5.2–5.3: full snapshot per launcher +
   generation id + per-launcher health) and implement it with **Game_shelf as the
   source**, pushing to a new orchestrator ingest endpoint modelled on the proven
   `prefill_exclusions.py:131-199` reconcile pattern. Push, not pull, per ADR-0016 §5
   condition 1 — the orchestrator never calls Game_shelf.
4. **Build the orchestrator consumer end** (§5.9): ingest + `owned=0` writer +
   `platforms.last_sync_at` writer + the three other enumeration-site fixes.
5. **Build the standalone service only when a trigger fires** (§7 Alternative A lists
   them), to the specification in §5 — which exists so the eventual build cannot
   quietly drop the hard parts (revocation, HITL, credential trust boundary).

This lands where the independent review of ADR-0017 landed, and it should: with two
consumers, an already-working bidirectional integration (§1 C6), near-agreeing data
(§1 C8), and a bug whose fix is mostly consumer-side (§2.2), a third deployable buys
process isolation nobody currently needs at the price of a new auth surface, a
migration hazard (§6 step 5), and the largest credential concentration on the LAN.

---

## 5. Design — the service specification (binding if/when construction triggers)

Sections 5.2–5.8 also bind the interim Game_shelf-hosted implementation wherever they
apply (contract shape, crypto, rotation safety, consumer auth).

### 5.1 Boundary

The service owns: launcher credentials; authentication and token refresh; scheduled
sync for unattended launchers; attended workflows (OTP relay, token/cookie paste, file
import) for the rest; entitlement snapshots with generations; per-launcher health. It
owns **no** notion of "prefillable", "displayable", "wanted", or cross-launcher game
identity (Game_shelf's `game_id` linking is enrichment, not launcher data — it stays
in Game_shelf; see OQ3). Implementation language Node, so adapters move — but this is
**not** lift-and-shift: C3 and C4 (§1) require rewriting Epic post-processing against
the service's own schema and formalising the record shape (§5.2) before anything
moves.

### 5.2 Entitlement record

The record makes the current implicit contract explicit rather than pretending
neutrality (C4):

```
{ launcher, launcher_game_id, title, playtime_minutes,
  attributes: { epic_namespace?, epic_catalog_id?, sandbox_type?, gog_slug?, ... },
  first_seen_generation, last_seen_generation }
```

Per-attribute merge semantics are **declared in the contract**, not buried in an
upsert: each attribute is either *authoritative* (absent ⇒ null; Epic's fields) or
*supplementary* (absent ⇒ keep prior; `gog_slug`, today's `syncEngine.js:67`
COALESCE). Consumers apply the declared semantics; the service stores last-observed
values per generation.

### 5.3 Revocation — full snapshot + generation id, not cursors

**Chosen:** per-launcher **full snapshot per completed sync**, tagged with a
monotonically increasing `generation`. An entitlement with
`last_seen_generation < launcher.current_generation` is revoked — tombstone by
absence, made explicit in reads (`owned: false, revoked_in_generation: N`), never a
mutable flag.

- **A generation commits only if the adapter ran to completion.** Adapters must throw
  on partial results (the §3.4 fix); a failed or partial run produces **no new
  generation**, so absence can never be misread as revocation. This turns the current
  worst silent-failure mode into a structurally impossible one.
- **Reads are full-set per launcher** with the generation id; `ETag`/`If-None-Match`
  on the generation makes polling free. Library sizes (~1–2k rows per launcher) make
  snapshots trivially cheap; incremental cursors solve a problem this data does not
  have, and C5 showed they cannot express revocation here anyway.
- **Consumers reconcile full-set**, exactly the proven pattern of
  `prefill_exclusions.py:131-199`: transactional insert-missing + delete-stale (here:
  mark-unowned, never delete — ADR-0016 §5 condition 3: ownership never deletes
  cache), scoped by source, bounded (50k, `prefill_exclusions.py:69`), authenticated.
- Push (interim) and pull (service) carry the identical payload — snapshot +
  generation + freshness — so the transport can flip without a contract change (OQ5).

### 5.4 Human-in-the-loop launchers are first-class states

Per-launcher auth state machine: `ok` | `attention_required` | `disabled`, where
`attention_required` carries `kind: paste_token | paste_cookie | email_otp |
file_import`, `since`, and operator instructions. Exposed on `/v1/health`; the
existing NAS alert-monitor pattern can subscribe.

- The **OTP relay** ports Game_shelf's two-phase flow (`awaiting_otp` + 5-minute
  window, `routes/sync.js:37-76`) as service endpoints.
- **File import** ports the Amazon JSON and Ubisoft cache-pair endpoints
  (`routes/launchers.js:48,67,116`) including the sync-lock that protects imported
  rows from the owned=0 sweep.
- **Honest freshness:** for attended launchers, staleness is bounded by operator
  attention, not by the scheduler. Consumers must treat `attention_required` like
  staleness — degrade and flag, never treat the stale snapshot as false revocation
  (generations already guarantee the latter).
- EA's 1-hour non-refreshable token means EA is effectively *manual-sync-only*; the
  service must present it that way rather than scheduling guaranteed failures.

### 5.5 Credential write/management API and trust boundary

Two planes, separately authenticated:

- **Read plane** (`/v1/entitlements`, `/v1/health`): per-consumer tokens (§5.8).
  Never returns credential material. This is the only plane consumers get.
- **Admin plane** (`/v1/admin/launchers/{id}/credentials` PUT/DELETE, `/otp` POST,
  `/import` POST): operator-only — LAN/loopback source allowlist (the orchestrator's
  `SourceAllowlistMiddleware` pattern) plus a distinct admin credential. Credentials
  are **write-only at the API**: no endpoint ever returns decrypted material, and
  reads return metadata only (`configured: true, kind, updated_at, key_id`).
- **The plaintext-secret QR endpoint dies.** Game_shelf's
  `GET /api/setup/qr/:launcher_id` returns a decrypted `totp_secret` (`setup.js:41-59`)
  to any authenticated session. It is not ported. Nothing is lost: the TOTP generators
  have zero production callers and the only launcher declared `credentials+totp` that
  would use them is the unimplemented Battle.net stub (C1). If TOTP enrolment is ever
  real, the QR is rendered **once, at credential-submission time, from the submitted
  plaintext** — never re-derivable from the store. The Game_shelf endpoint should be
  removed in step 2 of §6 regardless.
- **Ubisoft's password** (C1) is the only long-lived password in the system. Default:
  store session artifacts only (`ticket`, `rememberMeTicket`); when the rememberMe
  chain dies, re-auth is attended (it already requires an emailed OTP, so the operator
  is present to retype the password). Persistent password storage becomes an explicit
  per-launcher opt-in, surfaced on `/v1/health` as `stores_password: true`.

### 5.6 Cryptography (prerequisite: exists and is tested BEFORE any migration)

- **Key:** a raw 32-byte key (hex/base64) from a secret file or env var. If a
  passphrase must be supported, derive with `crypto.scrypt` (N=2^17, r=8, p=1,
  per-store random salt persisted beside the DB) — scrypt over Argon2id to avoid a
  native dependency; either is acceptable, unsalted single-pass SHA-256 (C7) is not.
- **AAD binding:** GCM AAD = `(launcher_id, key_id, schema_version)`, so a ciphertext
  cannot be replayed into a different launcher row or across key epochs.
- **Envelope with `key_id`** on every blob, and a **rotation routine**: introduce new
  key alongside old, re-encrypt row-by-row (decrypt-old → encrypt-new, transactional,
  verified), then retire the old key. Rotation is a tested code path, not a wiki page.
  Today, changing `GAMESHELF_ENCRYPTION_KEY` bricks every credential; that property
  must be gone before any credential moves anywhere (§6 step 2).

### 5.7 Token-rotation safety

Epic and GOG refresh tokens are **single-use-rotating** (`epic.js:96-103`,
`gog.js:79-84`): using one invalidates it and issues a successor. Three rules:

1. **Per-launcher mutex** (single-flight) around refresh — two concurrent refreshes
   mean one caller wins and the other's stored token is dead.
2. **Persist-before-use:** the new token pair is committed to the store before the
   first API call that uses it. A crash between refresh and persist otherwise loses
   the session.
3. **Previous-token slot:** retain the immediately-prior refresh token
   (`prev_refresh_token`, one generation). Some IdPs honour a grace window; at
   minimum it distinguishes "rotated and lost" from "revoked upstream" during
   incident response, and it narrows the §6 step-5 replay hazard.

### 5.8 Service ↔ consumer auth

Per-consumer static tokens, hashed at rest, **scoped** (`entitlements:read`,
`health:read`; admin is a different credential on a different plane). Never a single
shared token: the counterexample to avoid is `ORCH_TOKEN`, one unscoped static bearer
that today authenticates both the control-plane API and the data-plane agent
(`api/main.py:182-185` hands `settings.orchestrator_token` to the `AgentClient`;
`agent/app.py:156` validates with the same `BearerAuthMiddleware`) and authorizes
destructive cache purge. Transport: LAN-bind + source allowlist at minimum, mirroring
the orchestrator's existing middleware.

### 5.9 What consumers must still build (the service removes none of this)

- **Orchestrator:** an ownership ingest endpoint (full snapshot + generation +
  freshness per §5.3, reconciling into `games`); an `owned = 0` writer and a
  `platforms.last_sync_at` writer (§2.3 — both currently nonexistent); rejection /
  loud flagging of stale or `attention_required` feeds (ADR-0016 §5 condition 2); a
  migration if any non-Steam/Epic launcher is ever ingested (`platforms` CHECK,
  §2.3) — which should not happen until a consumer-side use exists (§3.5); and the
  three remaining enumeration-site fixes (§2.2): manifest-fetch targets for
  owned-but-never-prefilled games, the prune/restore block, and the decision of how
  ownership feeds the host cron's `selectedAppsToPrefill.json` (Steam prefill remains
  host-cron-owned per Piece 2).
- **Game_shelf:** becomes a consumer of the same contract; keeps a local last-known-
  good snapshot and degrades softly, the pattern it already implements for the
  orchestrator (`orchestrator.js:6-43`, `manualCoverageSnapshot.js:37-44`).

---

## 6. Migration sequence — every step independently valuable and reversible

| Step | What | Value on its own | Reversibility |
| --- | --- | --- | --- |
| 1 | Fix partial-sync bug in Game_shelf adapters (throw on partial page; `epic.js:126-146`, `humble.js:70-72`); adopt "no generation on failure" semantics in `syncEngine`. *In progress on `fix/launcher-partial-library-data-loss` at time of writing (uncommitted).* | Stops silent mass un-owning today | Code revert |
| 2 | Crypto hardening in place (§5.6: KDF/raw key, AAD, key-id envelope, tested rotation) + remove the plaintext QR endpoint (§5.5) | Closes C7 for the credentials where they already live | Blobs re-encryptable in both directions while both paths exist; endpoint removal loses nothing (zero callers) |
| 3 | Ownership contract v1 exposed **from Game_shelf** (snapshot + generation + health, §5.2–5.4 semantics), pushed to a new orchestrator ingest endpoint built on the `prefill_exclusions.py:131-199` pattern | ADR-0016 Option C realized; sync failures observable for the first time | Config-flag off; orchestrator falls back to the deployed `--recently-purchased` stopgap (ADR-0016 Option A) |
| 4 | Orchestrator consumer end (§5.9): ingest, revocation writer, `last_sync_at`, staleness rejection, and the three other enumeration-site fixes | **This is the step that fixes the motivating bug** | Config-flag off per sub-feature; `owned` flips are soft (never deletes cache, ADR-0015/0016) |
| 5 | *(Trigger-gated)* Stand up the service: same adapters, same contract, Game_shelf becomes consumer #2; flip transport push→pull if desired (OQ5) | Process isolation, third-consumer readiness | See hazard below |
| 6 | *(Trigger-gated)* Credential migration, **per launcher, last** | Single credential custodian | **Asymmetric** — see hazard |

**Credential-migration hazard (step 6):** moving credentials is *not* trivially
reversible. Epic/GOG refresh tokens rotate on use, so an encrypted blob copied at time
T is **dead** the moment the old deployment performs one refresh after T — and
replaying a stale blob back during rollback invalidates the live session the same way.
Required protocol, per launcher: freeze syncs at the source (`sync_locked` exists for
this), export/re-key the blob, verify one live refresh **from the service**, then
disable the source launcher. Rollback is the same protocol in reverse — or an attended
re-auth, which for five of ten launchers is the routine flow anyway. API-key launchers
(steam/xbox/itchio) migrate and roll back trivially; EA is moot (1-hour tokens);
Amazon/Ubisoft-full are files, re-importable at will.

---

## 7. Alternatives considered

### A. Do not build the service — incremental only (ADOPTED, as steps 1–4)

The strong form, stated fairly: steps 1–4 deliver **every user-visible outcome** —
motivating bug fixed, silent un-owning fixed, credentials hardened, ownership
explicit with revocation and freshness, sync failures observable — with **zero new
deployables**, no new auth surface, no credential concentration, and no rotating-token
migration hazard. The two apps already integrate in both directions with graceful
degradation (C6), their data already agrees (C8), and the biggest work item
(orchestrator consumer end, §2.2/§5.9) is identical under both topologies. An
independent review of ADR-0017 reached this conclusion; on the verified facts, it is
correct today, and this ADR adopts it.

**What would have to be true for it to lose** (the construction triggers for steps
5–6 — any one suffices, deliberately concrete):

- **T1 — a third consumer exists**, with a named need for entitlements or launcher
  auth that cannot reasonably be served through Game_shelf's contract endpoint.
- **T2 — the contract measurably constrains Game_shelf**: sync workload or the
  contract's availability requirements demonstrably degrade its primary job (UI +
  library), or its single-writer SQLite becomes a real contention point.
- **T3 — credential custody must leave Game_shelf**: a security requirement or
  incident makes hosting nine launchers' credentials inside the LAN-exposed UI app
  unacceptable, independent of consumer count.
- **T4 — lifecycle divergence**: Game_shelf needs to be down, migrated, or replaced
  while ownership data must keep flowing.

Absent a trigger, building the service is speculative generality; ADR-0017's own §6
conceded the one-consumer version of this point before proposing to build anyway.

### B. Shared library vendored into both apps

Rejected. A library cannot hold credentials once, cannot run a schedule once, cannot
host attended workflows — which is most of the value — and the consumers are in two
runtimes (Node adapters, Python orchestrator), so "vendored into both" is not even
mechanically available. (ADR-0017 OQ5 half-recognised this.)

### C. Orchestrator-native Steam ownership client only (ADR-0016 Option B/D)

A direct `GetOwnedGames` client keeps the orchestrator standalone and is the shortest
path for the single platform that matters most. Not adopted as the *whole* answer:
it needs a new Steam credential + privacy-settings handling, and it still requires
all of §5.9's consumer-side work while leaving health/attended-auth observability
unbuilt. Remains a legitimate fallback if the Game_shelf contract (step 3) proves
unreliable in practice — ADR-0016 Option D's hybrid stays open.

### D. Build the full service now

Rejected on the verified facts: the ambition ceiling is lower than advertised (§3 —
five attended launchers, two file-import, one stub, prefill credentials excluded); the
bug-fixing work is mostly consumer-side (§2.2); extraction is not lift-and-shift (C3,
C4); the security prerequisites (§5.6) are identical in-place; and the credential
migration carries the §6 hazard. What survives from ADR-0017 is the destination shape
— specified in §5 so it is buildable on trigger without redesign.

---

## 8. Consequences

- The motivating bug's fix is decoupled from any new deployable: steps 1–4 proceed now.
- Game_shelf temporarily gains a responsibility (contract host) that ADR-0017 argued
  it "never asked to own". That is real and is the standing cost of Alternative A;
  T1/T2 exist precisely to cap it.
- The orchestrator's Steam `library_sync` becomes a consumer of an explicit ownership
  source; ADR-0016's invariant holds; the corrected comment at `scheduler/jobs.py:295`
  stays correct.
- Revocation becomes expressible end-to-end (generations → `owned=0` writer) for the
  first time; the 616 + residue rows (ADR-0016 §1.4) become reconcilable in step 4
  ("reconcile, then prune" — ADR-0016 §5 condition 4).
- If a trigger fires, the service is built to §5 with the migration discipline of §6 —
  the design cost has been paid here, once.
- If nothing at all is done beyond today: the deployed `--recently-purchased` stopgap
  keeps new Steam purchases visible, the partial-sync bug keeps silently un-owning
  libraries on transient Epic/Humble errors, and the credential store keeps its C7
  weaknesses. Step 1 and step 2 are urgent independent of everything else.

---

## 9. Open questions

- **OQ1 — Contract custody long-term.** If no trigger ever fires, Game_shelf hosts
  the ownership contract indefinitely. Acceptable, or is that itself a slow-burn T2?
- **OQ2 — Ubisoft password.** §5.5 defaults to session-artifacts-only with attended
  re-auth. Confirm the operator accepts occasional OTP+password re-entry over
  persistent plaintext-password-in-blob storage.
- **OQ3 — Epic post-processing ownership.** `nestDLC`/`resolveCodenames` write
  Game_shelf's tables and need a live Epic token (C3). Split: catalog *facts*
  (real titles, namespace grouping inputs) belong with the sync; *presentation*
  (parent/child nesting in `game_editions`) stays consumer-side. Where exactly is the
  cut, and who calls the catalog API?
- **OQ4 — Classification facts.** The orchestrator holds `steam_app_info`
  (type/name/categories); Game_shelf holds `edition_tiers`. Which classification
  facts, if any, travel inside the contract versus remain consumer-local? (ADR-0017 §3
  wanted them centralized; that dependency is not needed for steps 1–4.)
- **OQ5 — Push vs pull at step 5.** Steps 3–4 use push (ADR-0016 §5 condition 1: the
  orchestrator never calls Game_shelf). A standalone service inverts to consumer pull.
  The payload is identical (§5.3); confirm the orchestrator ingest is built as
  transport-agnostic internals (ingest function fed by either its endpoint or a future
  puller) so the flip is config, not code.
- **OQ6 — Steam selection feed.** Ownership-driven Steam prefill ultimately means
  writing `selectedAppsToPrefill.json` (host cron owns Steam, Piece 2). Does the
  reconcile/persist path (`scheduler/jobs.py:305-317`) become ownership-driven, or
  does the host cron's `--recently-purchased` remain the sole additive mechanism?

---

## References

**Prior ADRs:** [ADR-0015] (purge is operator-driven, reversible; house format
reference) · [ADR-0016] (invariant; evidence; the counts in §1 C8) · [ADR-0017]
(rejected draft this replaces).

**Game_shelf** (`backend/src/`): `services/launchers/battlenet.js:21-24` ·
`routes/launchers.js:12-21,48,67,116` · `services/launchers/ubisoft.js:41-160,164,172-182,229-237` ·
`routes/sync.js:5,37-76` · `utils/totp.js:8,46` · `services/launchers/epicCatalog.js:15,67` ·
`services/syncEngine.js:57-69,113-133,145-159` · `services/launchers/epic.js:96-103,126-146` ·
`services/launchers/humble.js:70-72` · `services/launchers/amazon.js:32-34` ·
`services/launchers/gog.js:64-88` · `services/launchers/steam.js:28-39` ·
`utils/encrypt.js:15-23` · `routes/setup.js:41-59` ·
`services/crossLauncherExclusions.js:45-59` · `server.js:5-7,102-111` ·
`services/orchestrator.js:6-68` · `services/manualCoverage.js:200-216` ·
`services/manualCoverageSnapshot.js:37-44` · `package.json:12-23` (unpinned ranges).

**lancache_orchestrator** (`src/orchestrator/`):
`api/routers/prefill_exclusions.py:69,131-199` · `jobs/handlers/library_sync.py:103` ·
`jobs/handlers/prefill.py:131-133,324` · `scheduler/jobs.py:158-175,295-317` ·
`platform/steam/manifest_fetcher.py:115-137` · `db/migrations/0001_initial.sql:15-16,33-50` ·
`api/main.py:182-185` · `agent/app.py:156` · `core/settings.py:69,90`.

[ADR-0015]: 0015-operator-driven-cache-purge.md
[ADR-0016]: 0016-ownership-as-an-explicit-input.md
[ADR-0017]: 0017-shared-ownership-service.md
