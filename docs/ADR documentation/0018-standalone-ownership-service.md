# ADR-0018: Standalone Ownership + Launcher-Auth Service — Build Phased, Contract First

<!-- Last Updated: 2026-08-17 -->

**Status:** Proposed — 2026-08-17, **revised in place the same day (v2)**. Replaces
ADR-0017, which was **rejected by adversarial review** for the factual errors
catalogued in §1 and has since been **removed from the repository** at the operator's
direction — its errors are preserved in §1 below, so the record of what was wrong
survives without the flawed document itself. The number 0017 is retired, not reusable. **Why v2:** v1's §3/§4 answered a *misstated* requirement — the
misstatement was in the brief given to the ADR's author, not in the operator's ask —
and its decision leaned on refuting claims the operator never made (§1 R1). The
engineering specification (§5) survives from v1 largely intact; the scope analysis
(§3), decision (§4), migration (§6), and alternatives (§7) are re-derived here against
the requirement as the operator actually stated it:

> "The shared service was a source of truth. It would function exactly as it does for
> gameshelf. I don't need it to have 1 login for everything, just be a single docker
> container that connects to all the services (with help when needed) or has the lists
> for services that can't be connected to, for any other system to connect to and get
> the needed info and possibly be able to use it for logging in to download games."

Three things in that sentence invalidate v1's framing: **attended re-auth is accepted**
("with help when needed"); **file-import lists are first-class**, not an exception
("has the lists for services that can't be connected to"); and the service is
**"possibly" a login/credential broker for downloads**, not only a discovery source.

This ADR retains [ADR-0016]'s invariant — *ownership is an explicit input, never
inferred from cache contents* — and resolves ADR-0016 §5's open recommendation. Spans
`lancache_orchestrator` and `Game_shelf`; a third deployable is specified (§5) and,
unlike v1, its construction is **committed, phased** (§4), no longer trigger-gated.

**Decision in one line:** adopt the ownership *contract* now; keep the in-place
prerequisites (partial-sync fix — already merged; credential-store hardening — owed);
then build the standalone service in phases — **Phase A** ownership + lists + **its own web management UI** (the
"functions exactly as it does for gameshelf" bar, queryable by any consumer, and
configurable without depending on any consumer's UI),
**Phase B** an Epic token broker for download auth, and **Steam badge custody**
(§5.11 v3 — decided 2026-08-19, superseding the "Phase C, explicitly uncommitted"
position this paragraph originally recorded) — because the restated requirement plus the
orchestrator's own verified Epic-credential duplication (§2.4) supply the second
consumer need v1's trigger gate was waiting for.

Every file:line citation in the live sections (§2–§9) was verified on 2026-08-17
against `lancache_orchestrator` main @ 7dae4fe and `Game_shelf` **origin/master @
78875e6** (which includes the merged partial-sync fix PR #24, the sync-status
normaliser PR #25, and the sync-health classifier PR #26). Line numbers drift; treat
the cited *code*, not the number, as the reference.

---

## 1. Corrections register — why ADR-0017 was rejected

ADR-0017's design rested on claims that are false in the code. They are recorded here
because each one changes the design, and because this ADR must not be trusted on the
same points without re-verification.

| # | ADR-0017 claim | Verified reality |
| --- | --- | --- |
| C1 | "Battle.net stores `totp_secret`, so 2FA is generated, not prompted" — headless sync throughout | Battle.net is a **stub**: `battlenet.js:21-24` returns `[]` with a warning; registered `implemented: false` (`routes/launchers.js:19`). It stores nothing. `generateTOTPCode`/`generateSteamCode` (`utils/totp.js:8,46`) have **zero production callers** — `setup.js:3` imports only `generateQRSetupData`. The launcher that actually holds a password is **Ubisoft**: HTTP Basic login at `ubisoft.js:164`, and `refreshIfNeeded` re-persists the **plaintext password inside the credential blob on every sync** (`ubisoft.js:229-237`). |
| C2 | Headless capability implied for all launchers ("`syncAll(db)` runs on a scheduler") | Ubisoft's 2FA is an **emailed code** (`OTP_REQUIRED`, `ubisoft.js:172-175`) relayed through a two-phase job (`awaiting_otp`) with a **5-minute window** (`routes/sync.js:6,94-97`). It cannot run unattended. See §3's launcher table: ~5 of 10 registered launchers need a human to (re)auth. |
| C3 | "Launcher → DB coupling: none — `grep this.db` is 0" | The grep is misleading. `epicCatalog.js` takes a raw `db` handle as a **positional parameter** (`nestDLC(db, launcherId)` :15; `resolveCodenames(db, launcherId, session)` :67) and writes Game_shelf's `game_editions` and `games` tables directly, invoked from `syncEngine.js:176-191`, and additionally needs a **live Epic access token**. This is the single hardest extraction blocker. |
| C4 | "Output is already a neutral DTO — `{launcher_game_id, title, playtime_minutes}`" | The return shape carries ~7 fields bound **1:1 to `game_editions` column names** (`epic_namespace`, `epic_catalog_id`, `sandbox_type`, `gog_slug`, `playtime_minutes`, …) with **per-field merge semantics** in the upsert (`syncEngine.js:66-78`): `gog_slug` is COALESCEd (:76) while the Epic fields overwrite with NULL. It is Game_shelf's schema wearing a DTO costume. |
| C5 | `since=` cursor for incremental pulls | There is **no `updated_at` column** on `game_editions` to build a cursor from, and a cursor cannot express "no longer owned" — revocation today is a soft flag flipped by full-set comparison (`syncEngine.js:122-165`). The cursor design was unimplementable as written. |
| C6 | "Neither app depends on the other; a shared service decouples them" | They **already integrate bidirectionally in production with graceful degradation**: push via `crossLauncherExclusions.js:45-59` on the daily cron (`server.js:102-108`) into the orchestrator's transactional reconcile endpoint (`api/routers/prefill_exclusions.py:140-200`); pull via `orchestrator.js:47-68` and `manualCoverage.js:200-216`; soft-503 + last-known-good already implemented. "Decoupling" is not a benefit on offer. |
| C7 | "Credentials at rest … fails closed" presented as adequate | `encrypt.js` is AES-256-GCM but derives the key as **unsalted single-pass SHA-256(passphrase)** with only a length ≥ 32 check (:15-23); there is **no key-rotation routine** (changing the key bricks every credential) and **no AAD binding**. Separately, `GET /api/setup/qr/:launcher_id` returns the **decrypted plaintext `totp_secret`** to any authenticated session (`setup.js:41-59`). |
| C8 | 2508 vs 1115 shows "two classifiers, two answers" — divergence as motivation | The 2508 is **licence residue** (dlc/demos/tools/unclassified) from the deleted sync worker (ADR-0016 §1.4). Real game counts agree within ~2%: ≈1138 (orchestrator) / 1126 (Steam) / 1115 (Game_shelf). The systems do **not** meaningfully disagree; disagreement must not be used to justify anything. |
| C9 | `library_sync.py:89` as the circular enumeration | The enumeration is at `jobs/handlers/library_sync.py:103`. Both prior ADRs cite the wrong line. More importantly, it is only **one of four** cache-derived enumeration sites (§2.2). |

**R1 — revision note (v2).** ADR-0018 v1 itself belongs in this register: its §3
"CANNOT #1" refuted *"unattended everything"* — a requirement the operator never
stated ("with help when needed" was in his own words) — and that refutation was
load-bearing in v1's §4 decision to gate construction. Its §3 "CANNOT #2" declared
prefill-credential consolidation categorically impossible; that is **false for Epic**
(§5.10 — the orchestrator's Epic auth is an OAuth refresh chain behind a single choke
point) and **overstated for Steam** (§5.11 v3 — the "file-based, therefore not
broker-able" reasoning was itself wrong; custody is now decided). v1 also treated
file-import launchers as a limitation rather than a deliverable. The v1 §5
engineering survives; everything downstream of the misstated question was re-derived
in this revision. Register rows C1–C9 cite the
Game_shelf tree as reviewed at rejection time (03cd89f); PRs #24–#26 have since
drifted line numbers in `epic.js`, `humble.js`, `syncEngine.js`, and
`routes/sync.js` — live sections cite current refs, updated where drift occurred.

---

## 2. Context — the problem actually on the table

### 2.1 The motivating bug (ADR-0016, restated)

The orchestrator inferred Steam ownership from cache contents: a newly purchased game
had no `games` row and was invisible to sweep/validate/prefill forever. The invariant
fix — ownership as an explicit input — is adopted and not in question here. The
deployed `--recently-purchased` stopgap keeps new Steam purchases visible in the
interim. The question is the *mechanism* — and, in v2, the *scope*: the operator's
requirement is broader than the bug (source of truth + lists + download login), so
the bug fix is a subset of the design, not its whole justification.

### 2.2 Feeding ownership in fixes one of FOUR enumeration sites

Any plan that treats "point `library_sync` at an ownership feed" as the fix does not
solve the motivating bug. The orchestrator derives state from cache contents in four
places:

| Site | What it does | Effect if unfixed |
| --- | --- | --- |
| `jobs/handlers/library_sync.py:103` | Populates `games` from `agent_client.prefilled_apps()` | The one an ownership feed fixes |
| `platform/steam/manifest_fetcher.py:108-118` | Agent self-enumerates fetch targets from `selectedAppsToPrefill.json` (:115) + `.bin`/`.shas` delta | An owned-but-never-prefilled game gets **no manifests**, so validate has nothing to stat |
| `scheduler/jobs.py:305-317` | Auto-classify/prune block reads `prefilled_apps()` to build the selection restore set | New games don't enter the host cron's selection except via the `--recently-purchased` stopgap |
| `scheduler/jobs.py:155-175` | Scheduled prefill enqueues **Epic only** (:165; Piece 2 — Steam prefill is host-cron-owned, by design) | Ownership ingest alone never causes a Steam byte to download |

An ownership feed without the other three yields **visible-but-inert rows**: a `games`
row exists; nothing fetches its manifests, nothing prefills it, validation has no
input. The consumer-side plumbing is a large share of the work and is required under
every topology, service or not. This fact survives v2 unchanged — it governs
*sequencing*, not *whether to build*.

### 2.3 The consumer side has no revocation or freshness today

`owned = 0` has **no writer anywhere in orchestrator `src/`** (the schema has the
column, `0001_initial.sql:38`, default 1). `platforms.last_sync_at` is **never
written** — every reference is a read (`api/routers/platforms.py:121,152`,
`cli/commands/auth.py:45`, `api/routers/status.py:264`). And `games.platform` is
CHECK-constrained to `('steam','epic')` via the `platforms` FK
(`0001_initial.sql:16,35`). Whatever produces ownership, the orchestrator currently
cannot *consume* revocation, cannot *record* freshness, and cannot *hold* a non-Steam,
non-Epic row. That work exists in every option below.

### 2.4 NEW (v2): two live Epic credential chains against one account — verified

The same Epic account currently has **two independent, simultaneously live refresh
chains**, live-verified by the operator on 2026-08-17/18:

- **Orchestrator:** `/var/lib/orchestrator/epic_session.json` — a plain JSON file
  holding `{"refresh_token": …}`, written mode 0600 (`platform/epic/oauth.py:98-116`;
  path from `core/settings.py:86`). `EpicClient._access_token` loads it, refreshes,
  and re-persists the rotated token (`platform/epic/client.py:60-88`, persist at
  :86-87), serialized by a single-flight lock (:55-58) because Epic refresh tokens
  are single-use rotating.
- **Game_shelf:** the `epic` row's encrypted `launchers.credentials_json`
  (AES-256-GCM under the C7-weak derived key) — `access_token`, `refresh_token`,
  `expires_at`, `refresh_expires_at`, `account_id`; refreshed and re-persisted each
  sync (`epic.js:70-113`, rotated blob at :96-103; persisted by
  `syncEngine.js:53-57`).

**What this verifiably is:** duplicated credential custody (one account's secrets in
two places under two different at-rest schemes — one hardened-key-less encrypted blob,
one plain 0600 JSON file) and duplicated re-auth burden (when a chain expires, the
operator pastes an auth code into that app; two apps, two pastes).

**What it is NOT claimed to be:** the two chains almost certainly originate from two
*separate* authorization-code grants (each app's onboarding takes its own pasted
code), and Epic supports concurrent sessions per account — so rotating one chain
probably does **not** invalidate the other. Mutual invalidation is **unverified**
(OQ7) and carries **no weight** in §4's decision. The duplication argues for a broker
on *custody hygiene and re-auth convenience* grounds only — a modest benefit, and §4
weighs it as such.

---

## 3. Scope — what the service CAN and CANNOT deliver under the real requirement

The bar is the operator's, not v1's: *"function exactly as it does for gameshelf"* —
attended steps included — plus queryable by any system, plus "possibly" download
login. The launcher population, verified per adapter:

| Launcher | Library source | Steady-state unattended? | Human needed to (re)auth |
| --- | --- | --- | --- |
| steam | Web API `GetOwnedGames` (+`include_played_free_games:1`), `steam.js:28-39` | Yes | No — durable API key |
| xbox | API key (`xbox.js:10,27-30`) | Yes | No |
| itchio | API key (`itchio.js:11,26,35`) | Yes | No |
| epic | OAuth; **single-use rotating** refresh token (`epic.js:96-103`); partial fetches now **throw** (:142-155, PR #24) | Yes, until the refresh chain breaks | Paste fresh auth code |
| gog | OAuth; **single-use rotating** refresh token (`gog.js:64-88`) | Yes, until refresh expiry | Paste fresh auth code |
| ea | Implicit-flow access token, **1 h expiry, no refresh** (`ea.js:71-99`) | **No** | Paste a new token, hourly |
| humble | Pasted browser session cookie (`humble.js:16-24`); incomplete order fetches now **throw** (:81-88, PR #24) | While the cookie lives | Paste fresh cookie |
| ubisoft | GraphQL (~16 native titles) + **operator file upload** for the full library (binary varint cache parser, `ubisoft.js:41-160`) | Partial (`rememberMeTicket`) | **Emailed OTP** in a 5-min window; file upload |
| amazon | **File import only** — `fetchOwnedGames` throws (`amazon.js:32-34`) | **No API exists** — and that is fine: the list IS the source | File upload (`routes/launchers.js:48,67`) |
| battlenet | **Unimplemented stub** (`battlenet.js:21-24`) | n/a | n/a |

**CAN deliver (and the requirement asks for):**

1. **Game_shelf-parity sync in one container** — API launchers on a schedule, OAuth
   chains with rotation safety, the two-phase OTP relay, and pasted-token/cookie
   flows, all with per-launcher health (`ok | attention_required | disabled`, §5.4).
   Since PR #26 Game_shelf itself classifies sync health
   (`services/syncHealth.js:32-80`, `GET /api/sync/health`, `routes/sync.js:43-66`);
   the service ports that classifier, it does not invent one.
2. **The lists, first-class.** For launchers with no connectable API (Amazon; the
   Ubisoft full library), an operator upload IS a completed sync: it produces a
   generation exactly as an adapter run does (§5.4). The upload surface is designed
   for a credential-holding process — parser isolation, §5.5.
3. **One queryable source of truth**: entitlement snapshots + generations +
   per-launcher freshness/health for *any* consumer (§5.2–5.3), not just the two
   apps that exist today.
4. **Epic download login, brokered** (§5.10). The orchestrator's Epic prefill auth is
   an OAuth refresh chain behind one choke point
   (`platform/epic/client.py:102-115` `_call_with_401_refresh`; token acquisition
   isolated in `_access_token` :60-88). The service can own the single refresh chain
   and issue short-lived access tokens to scoped consumers. This retires the §2.4
   duplication.
5. **One credential store with real key management** (§5.6) — an improvement over
   both of today's schemes.

**CANNOT deliver (honest limits that survive the re-framing):**

1. **Removing attended re-auth.** ~5 of 10 launchers structurally need a human
   (paste, emailed OTP, or file upload). The requirement accepts this — the service
   *hosts* the attended workflows and makes "needs help now" observable (§5.4); it
   does not eliminate them. (v1 wielded this as an argument against building; v2
   records it as a property, not an objection.)
2. **Brokering Steam download auth over HTTP.** SteamPrefill authenticates from
   `Config/account.config` — a file read by the binary (`core/settings.py:88-93`).
   **That is an integration constraint, not a transport one** (§5.11 v3 corrects this
   entry's original "there is no token to serve"): the file holds a persisted session
   artifact and is as serveable as any other small file. The options are the service
   owning/distributing that file, or hosting SteamPrefill itself. §5.11 v3 adopts the
   first — single custody, delivered by the agent that already mounts the store.
3. **The motivating bug, by itself.** §2.2: one of four sites. The orchestrator-side
   consumer work is topology-independent and proceeds in parallel (§6 step 4).
4. **Better data.** Same upstream APIs, same failure classes. The partial-sync
   data-loss bug v1 flagged is now **fixed and merged** (adapters throw:
   `epic.js:142-155`, `humble.js:81-88`; plus a defence-in-depth 20% unown-ratio
   guard with a 10-title floor, `syncEngine.js:5-12,142-153`). Moving the adapters
   moves the *fix* with them; the "no generation on failure" rule (§5.3) makes the
   same failure mode structurally impossible in the service.
5. **A consumer-side use for every launcher's ownership.** The orchestrator can only
   prefill Steam and Epic; Amazon/Humble/itch cache presence is already served by the
   manual-coverage flows (`manualCoverage.js`, orch `/api/v1/manual-downloads/*`).
   Holding all ten launchers' entitlements is now *the point* (source of truth for
   any future consumer) — but *ingesting* them into the orchestrator stays gated on
   an actual use (§5.9), so inventory never masquerades as capability.

---

## 4. Decision

**Build the standalone service, phased; adopt the contract and the in-place
prerequisites first.** This reverses v1's headline (build only on trigger) — not
because v1's engineering was wrong, but because the question was. What changed, named
precisely:

1. **The requirement was restated by the operator.** v1 §4 weighed the service as
   speculative generality against needs nobody had voiced. The operator has voiced
   them: a single container, source of truth, lists included, any-system reads,
   "possibly" download login. For a personal deployment, the product owner naming the
   capability is the requirement; the ADR's remaining job is cost, safety, and
   sequence — not whether anyone wants it.
2. **v1's own trigger condition is met in spirit.** T1 required a consumer need "that
   cannot reasonably be served through Game_shelf's contract endpoint." The
   orchestrator's Epic download credential (§2.4) is exactly that: the read plane
   never returns credential material by design (§5.5), and hosting *token issuance*
   inside the LAN-exposed UI app would enlarge precisely the blast radius a broker
   exists to shrink (§7 E). Two consumers with distinct needs now exist: Game_shelf
   (ownership for UI/identity) and the orchestrator (ownership + Epic download auth).
3. **v1's load-bearing objection dissolved.** "Unattended everything" was never
   asked; with it withdrawn, what remains against building are *costs* (extraction
   effort C3/C4, a new auth surface, credential concentration, migration care) — all
   real, all schedulable, none decision-reversing once the requirement is genuine.

**Weighed honestly, including against the build:**

- The **broker's measurable benefit today is modest**: one credential custodian
  instead of two, one attended re-auth paste instead of two when an Epic chain dies.
  The retracted mutual-invalidation risk (§2.4) is *not* counted. If that benefit
  does not justify Phase B's cost, Phase B is **severable** (OQ8) — Phase A alone
  satisfies "source of truth + lists + any-system reads."
- The **consumer-side work still dominates the bug fix** (§2.2) and proceeds
  regardless (§6 step 4). The service does not accelerate it and must not wait for it.
- **Extraction is not lift-and-shift** (C3/C4): the epicCatalog coupling must be cut
  inside Game_shelf before adapters move (§6 step 2).
- **Crypto hardening precedes any credential landing in the service** (§5.6, §6
  step 1) — building the biggest credential concentration on the LAN on an unsalted
  SHA-256 key derivation would trade Security (priority 1) for Speed (priority 6).

Concretely:

1. ~~Fix the partial-sync bug~~ — **done and merged** (Game_shelf PRs #24–#26:
   adapters throw, ratio guard, sync-health classifier + endpoint).
2. **Harden the credential store in place** (§5.6; retire the plaintext-`totp_secret`
   QR endpoint, §5.5). Identical work under every topology; prerequisite to any
   credential migration.
3. **Cut the epicCatalog coupling** (C3) inside Game_shelf: catalog *facts* with the
   sync, *presentation* (DLC nesting in `game_editions`) consumer-side (OQ3).
4. **Build Phase A of the service** — adapters, attended workflows, file-import
   lists, entitlement contract (§5.2–5.4), health, and the **management web UI**
   (§5.12: add/edit/delete platforms, logs, status board) — with Game_shelf as
   consumer #1. The UI is the largest single item here and is not optional: without
   it the service cannot be configured at all.
5. **Build the orchestrator consumer end** (§5.9) in parallel — ingest + `owned=0`
   writer + `last_sync_at` writer + the three other enumeration-site fixes. This is
   the step that fixes the motivating bug, and it does not wait for the service.
6. **Build Phase B** — the Epic token broker (§5.10) — after step 2's crypto exists
   and Phase A is stable; the orchestrator swaps its `_access_token` internals to the
   broker and retires `epic_session.json`.
7. **Steam badge custody (§5.11 v3) is decided** — single custodian, distinct
   badges, agent-delivered — and scheduled after Phase A. Only the *escrow to a third
   location* variant of the old Phase C remains uncommitted.

---

## 5. Design — the service specification (binding for construction)

Sections 5.2–5.8 and §5.12 bind Phase A; §5.10–5.11 bind Phase B/C. Where Game_shelf retains
credentials in the interim, §5.6–5.7 bind that store too.

### 5.1 Boundary

The service owns: launcher credentials; authentication and token refresh; scheduled
sync for unattended launchers; attended workflows (OTP relay, token/cookie paste,
**file import**) for the rest; entitlement snapshots with generations; per-launcher
health; and, in Phase B, **Epic access-token issuance to scoped consumers**. It owns
**no** notion of "prefillable", "displayable", "wanted", or cross-launcher game
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
*supplementary* (absent ⇒ keep prior; `gog_slug`, today's `syncEngine.js:76`
COALESCE). Consumers apply the declared semantics; the service stores last-observed
values per generation.

### 5.3 Revocation — full snapshot + generation id, not cursors

**Chosen:** per-launcher **full snapshot per completed sync**, tagged with a
monotonically increasing `generation`. An entitlement with
`last_seen_generation < launcher.current_generation` is revoked — tombstone by
absence, made explicit in reads (`owned: false, revoked_in_generation: N`), never a
mutable flag.

- **A generation commits only if the adapter ran to completion.** The merged fix
  already makes adapters throw on partial results; in the service, a failed or
  partial run produces **no new generation**, so absence can never be misread as
  revocation. The former worst silent-failure mode becomes structurally impossible
  (Game_shelf's interim ratio guard, `syncEngine.js:142-153`, remains as defence in
  depth until cutover).
- **A file import commits a generation** exactly like a completed adapter run
  (§5.4) — which is what makes import-only launchers first-class rather than
  sync-locked exceptions.
- **Reads are full-set per launcher** with the generation id; `ETag`/`If-None-Match`
  on the generation makes polling free. Library sizes (~1–2k rows per launcher) make
  snapshots trivially cheap; incremental cursors solve a problem this data does not
  have, and C5 showed they cannot express revocation here anyway.
- **Consumers reconcile full-set**, exactly the proven pattern of
  `prefill_exclusions.py:140-200`: transactional insert-missing + delete-stale (here:
  mark-unowned, never delete — ADR-0016 §5 condition 3: ownership never deletes
  cache), scoped by source, bounded (50k, `prefill_exclusions.py:69`), authenticated.
- The payload is transport-agnostic — snapshot + generation + freshness — so push
  (service → orchestrator ingest) and pull (consumer → service) carry identical
  content (OQ5).

### 5.4 Attended workflows and file imports are first-class states

Per-launcher auth state machine: `ok` | `attention_required` | `disabled`, where
`attention_required` carries `kind: paste_token | paste_cookie | email_otp |
file_import_due`, `since`, and operator instructions. Exposed on `/v1/health`; the
existing NAS alert-monitor pattern can subscribe. The classifier ports Game_shelf's
merged `syncHealth.js` (staleness derived from last-completed-sync age, never from
job status alone — its motivating failure was an `awaiting_otp` job ageing silently).

- The **OTP relay** ports Game_shelf's two-phase flow (`awaiting_otp` + 5-minute
  window, `routes/sync.js:69-108`) as service endpoints.
- **File import** ports the Amazon JSON preview/import and Ubisoft cache-pair
  endpoints (`routes/launchers.js:48,67,116`). An accepted import **commits a
  generation** (§5.3); revocation for import-only launchers can therefore only occur
  on a *newer import*, which replaces today's `sync_locked` protection with a
  structural guarantee. Import staleness is surfaced as `file_import_due` (advisory —
  an old list is stale, not wrong).
- **Honest freshness:** for attended launchers, staleness is bounded by operator
  attention, not by the scheduler. Consumers must treat `attention_required` like
  staleness — degrade and flag, never treat the stale snapshot as false revocation
  (generations already guarantee the latter).
- EA's 1-hour non-refreshable token means EA is effectively *manual-sync-only*; the
  service must present it that way rather than scheduling guaranteed failures.

### 5.5 Credential write/management API, trust boundary, and parser isolation

Two planes, separately authenticated:

- **Read plane** (`/v1/entitlements`, `/v1/health`; Phase B adds `/v1/broker/*`,
  §5.10): per-consumer tokens (§5.8). Never returns stored credential material.
- **Admin plane** (`/v1/admin/launchers/{id}/credentials` PUT/DELETE, `/otp` POST,
  `/import` POST): operator-only — LAN/loopback source allowlist (the orchestrator's
  `SourceAllowlistMiddleware` pattern) plus a distinct admin credential. Credentials
  are **write-only at the API**: no endpoint ever returns decrypted material, and
  reads return metadata only (`configured: true, kind, updated_at, key_id`).
- **Parser isolation (addresses security-review F7 — upload parsers inside a
  credential-holding process).** Imported files are parsed in a **separate
  unprivileged child process** with no key material in its environment, a hard input
  size cap, a kill timeout, and JSON-over-stdout as its only output channel; the
  service process never feeds attacker-controlled bytes to an in-process parser. The
  binding case is Ubisoft's binary varint cache parser (`ubisoft.js:41-160`) — a
  hand-rolled binary decoder is exactly the code that must not share an address space
  with a credential store. Amazon's JSON parse rides the same harness for uniformity.
  The surface is admin-plane and allowlisted, so the realistic threat is a crafted
  file, not a remote attacker — the isolation is proportionate, not theatre.
- **The plaintext-secret QR endpoint dies.** Game_shelf's
  `GET /api/setup/qr/:launcher_id` returns a decrypted `totp_secret` (`setup.js:41-59`)
  to any authenticated session. It is not ported. Nothing is lost: the TOTP generators
  have zero production callers and the only launcher declared `credentials+totp` that
  would use them is the unimplemented Battle.net stub (C1). If TOTP enrolment is ever
  real, the QR is rendered **once, at credential-submission time, from the submitted
  plaintext** — never re-derivable from the store. The Game_shelf endpoint should be
  removed in §6 step 1 regardless.
- **Ubisoft's password** (C1) is the only long-lived password in the system. Default:
  store session artifacts only (`ticket`, `rememberMeTicket`); when the rememberMe
  chain dies, re-auth is attended (it already requires an emailed OTP, so the operator
  is present to retype the password). Persistent password storage becomes an explicit
  per-launcher opt-in, surfaced on `/v1/health` as `stores_password: true`.

### 5.6 Cryptography (prerequisite: exists and is tested BEFORE any credential lands)

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
  must be gone before any credential moves anywhere (§6 step 1).

### 5.7 Token-rotation safety

Epic and GOG refresh tokens are **single-use-rotating** (`epic.js:96-103`,
`gog.js:79-84`; the orchestrator's Epic client already implements single-flight for
the same reason, `client.py:55-58`). Three rules:

1. **Per-launcher mutex** (single-flight) around refresh — two concurrent refreshes
   mean one caller wins and the other's stored token is dead.
2. **Persist-before-use:** the new token pair is committed to the store before the
   first API call that uses it. A crash between refresh and persist otherwise loses
   the session.
3. **Previous-token slot:** retain the immediately-prior refresh token
   (`prev_refresh_token`, one generation). Some IdPs honour a grace window; at
   minimum it distinguishes "rotated and lost" from "revoked upstream" during
   incident response.

### 5.8 Service ↔ consumer auth

Per-consumer static tokens, hashed at rest, **scoped**: `entitlements:read`,
`health:read`, and (Phase B) `epic:token` — issuance scopes are never bundled into
read scopes, and admin is a different credential on a different plane. Never a single
shared token: the counterexample to avoid is `ORCH_TOKEN`, one unscoped static bearer
that today authenticates both the control-plane API and the data-plane agent
(`api/main.py:184` hands `settings.orchestrator_token` to the `AgentClient`;
`agent/app.py:156` validates with the same `BearerAuthMiddleware`) and authorizes
destructive cache purge. Transport: LAN-bind + source allowlist at minimum, mirroring
the orchestrator's existing middleware.

### 5.9 What consumers must still build (the service removes none of this)

- **Orchestrator:** an ownership ingest (full snapshot + generation + freshness per
  §5.3, reconciling into `games`); an `owned = 0` writer and a
  `platforms.last_sync_at` writer (§2.3 — both currently nonexistent); rejection /
  loud flagging of stale or `attention_required` feeds (ADR-0016 §5 condition 2); a
  migration if any non-Steam/Epic launcher is ever ingested (`platforms` CHECK,
  §2.3) — which should not happen until a consumer-side use exists (§3 CANNOT #5);
  the three remaining enumeration-site fixes (§2.2); and, in Phase B, the broker
  client swap (§5.10 — confined to `EpicClient._access_token`).
- **Game_shelf:** becomes a consumer of the same contract; keeps a local last-known-
  good snapshot and degrades softly, the pattern it already implements for the
  orchestrator (`orchestrator.js:6-43`, `manualCoverageSnapshot.js:37-44`); retains
  UI, enrichment, edition tiers, and cross-launcher identity; eventually sheds its
  sync engine and credential store (§6 steps 3/6).

### 5.10 Phase B — the Epic token broker (NEW in v2)

**What it is:** the service owns the **single** Epic refresh chain and issues
short-lived access tokens to scoped consumers.

- **Endpoint:** `POST /v1/broker/epic/token` (scope `epic:token`) → `{ access_token,
  token_type, account_id, expires_at }`. A `force_refresh` flag maps to the
  consumer's 401-retry path. The **refresh token never leaves the service** — the
  broker issues only the short-lived (~8 h) access tokens; §5.7's mutex,
  persist-before-use, and prev-token slot apply to the one chain it keeps.
- **Orchestrator integration is a seam swap, not a rewrite.** Token acquisition is
  already isolated in `EpicClient._access_token` (`client.py:60-88`), and every Epic
  call routes through `_call_with_401_refresh` (:102-115). Phase B replaces the body
  of `_access_token` (file-load + refresh + persist) with a broker call; the retry
  choke point, error taxonomy (`EpicNotAuthenticatedError`), and all callers are
  untouched. `epic_session.json` is then deleted.
- **Failure mode is loud, by policy:** broker unreachable ⇒
  `EpicNotAuthenticatedError` ⇒ the prefill/validate job fails with the existing
  taxonomy and the scheduler retries later. No cached-token fallback beyond the
  token's natural lifetime, no silent degradation. This *is* a new availability
  coupling — Epic prefill acquires a dependency on the service that it does not have
  today — accepted because prefill is retryable batch work, not interactive (OQ1
  covers placement to keep the coupling cheap).
- **Blast radius, stated plainly:** a consumer credential with `epic:token` yields
  live Epic access tokens — effectively the launcher client's account power
  (library, playtime, manifest/CDN auth) for the token lifetime. Mitigations:
  `epic:token` is granted to exactly one consumer credential (the orchestrator's);
  every issuance is audit-logged with consumer id + correlation id; tokens issued are
  short-lived only; the scope is revocable per-consumer without touching the chain.
  This is a real enlargement of what "read access" can mean and is the main standing
  security cost of Phase B.
- **What Phase B retires:** the §2.4 duplication — one custodian, one at-rest scheme
  (§5.6), one re-auth surface (`attention_required` on `/v1/health` when the chain
  dies, one paste to restore *both* consumers). **What it does not buy:** any
  correctness fix — the two chains today probably coexist without conflict (§2.4,
  unverified either way; OQ7).
- **Extensibility, not speculation:** GOG is the only other rotating-chain launcher;
  a `gog:token` twin is mechanical *if* a consumer ever needs GOG download auth. None
  does today; it is not built.

### 5.11 Steam credential custody — position restated (v3, 2026-08-19)

**Supersedes the v2 text of this section.** Two of its claims were wrong; correcting
them changed the conclusion.

**Correction 1 (factual).** v2 asserted that Steam's prefill auth *"is not a bearer
token and cannot be served over HTTP."* That is false. SteamPrefill persists a
**session artifact** in `Config/account.config` — 513 bytes on disk, understood from
prior investigation to be a SteamKit2 refresh-token JWT. **This session could not
confirm the encoding**: reading the file is blocked as a live credential, so the JWT
specifics are carried forward on the earlier note's authority, not re-verified (see
OQ6 of the fallback design, which depends on it). The correction stands either way —
a 513-byte file holding a persisted token is exactly as serveable as any other small
file. The real obstacles are renewal and custody semantics, below; transport was never
one of them.

**Correction 2 (inventory).** Every prior draft discussed one Steam credential. There
are **three**, at two privilege levels — verified against the live hosts 2026-08-18:

| # | Location | Kind | Observed | Power |
| --- | --- | --- | --- | --- |
| 1 | Game_shelf DB (LXC 1102), `launchers.credentials_json` | Web **API key** + `steamid64` (`steam.js:11-13`) | Does not expire | **Read-only** — `GetOwnedGames` and nothing else |
| 2 | NAS `lancache-host/SteamPrefill/Config/account.config` | SteamKit2 refresh-token JWT | 513 B, written 2026-04-14; prior copy `account.config.bak` 2025-07-28 | **Full account session** |
| 3 | NAS volume `depotdownloader-config`, .NET IsolatedStorage (`manifest_fetcher.py:66-71`) | DepotDownloader `-remember-password` session | 392 B, written 2026-07-01 — the PR #213 go-live 2FA | **Full account session** |

Credential #3 appears in no earlier draft of this ADR.

**Renewal is silent and self-healing.** #2's file was replaced exactly once between
2025-07-28 and 2026-04-14 (~261 days) with no operator action, and has not been
rewritten in the 126 days since — across roughly 500 logins, extrapolating the
observed cadence (`app.log` covers ~2 weeks and holds 56 × `Starting login!`, 54
successful, 2 transient `RateLimitExceeded` that self-recovered within six hours).
Each run exchanges the stored refresh token for a short-lived access token; the stored
token itself is renewed only as it nears expiry. **Consequence:** the unattended
re-auth cadence is roughly eight months, not the "~200 days, then attended 2FA" an
earlier session's note assumed. That note is superseded.

**Decision (operator, 2026-08-19): the service is sole custodian of all three, and the
badges stay distinct.**

- **Single custody, not a single credential.** All three live in the service's store.
  They are *not* merged into one shared token. #2 and #3 carry identical power, so
  merging saves no privilege and no operator attention, while handing two independent
  programs one mutable secret to race on — the §5.7 rotation hazard, and precisely the
  shape of the live Epic defect in §2.4. Distinct badges under one custodian is the
  same centralization without the coupling.
- **The API key is never lent.** The service calls `GetOwnedGames` itself and
  publishes the entitlement record (§5.2); no consumer ever receives #1. This is
  strictly stronger than brokering it, and it is what §5.12's *"no consumer ever
  touches credential material"* already requires.
- **Delivery for #2/#3 is checkout/check-in over the existing agent channel.**
  SteamPrefill and DepotDownloader are third-party .NET binaries with no
  credential-provider hook — their only integration surface is a file at a path they
  choose, so "reference it remotely" is not available. The agent already runs on the
  NAS with read-write mounts on both stores (`/SteamPrefill` rw,
  `/depotdownloader-config` rw), so it writes the badge locally immediately before a
  run and pushes any renewal back afterwards. No new secret channel, no new mount, and
  authentication is the token that already exists.
- **Phasing.** This supersedes v2's "Phase C, uncommitted" position on *custody*.
  Ownership (#1) remains Phase A. Custody of #2/#3 is scheduled after Phase A and
  ahead of the old Phase C, because the operator requirement is a single vault and the
  delivery plumbing already exists.

**Considered and held in reserve: replacing each tool's credential path with a network
mount of the service's store.** Designed in full rather than dismissed — see
`docs/superpowers/specs/2026-08-19-mapped-credential-mount-fallback-design.md`, which
is the adopted fallback should checkout/check-in prove unworkable. Not chosen now
because: renewal is believed to *replace* the file rather than rewrite it in place,
which would detach a single-file mount and force a whole-directory mount, dragging the
176 KB `successfullyDownloadedDepots.json` (rewritten every run) onto the share —
**an assumption, not a verified fact; see OQ6**; and it makes prefill availability
depend on the service being reachable across the `192.168.1.0/24` ↔ `10.100.23.0/24`
boundary, whose *reverse* direction already fails host-key verification LXC→NAS (the
mount needs NAS→service, which is untested). A full account session would also sit on
a network filesystem, the same substrate as the stale-attribute failures recorded
during the 2026-07/08 eviction investigation. **Note what is *not* a reason:** the
fallback design itself rejects NFS/`AUTH_SYS` and specifies encrypted SMB3, so
"cleartext on the wire" would be an objection to a design nobody proposed.

**What the service does not take on.** SteamPrefill remains a *data-plane* workload on
the NAS (re-arch ②/④); the service does not host it. Re-authentication stays attended
2FA on the host — a vault can hold and restore a badge, never mint one.

### 5.12 Management web interface (REQUIRED, Phase A — added by the operator)

The service ships **its own web management interface**, served from the same
container. Operator requirement, stated directly: *"a web based management interface
that will allow me to add, edit, delete, view logs, and see the status of all
platforms entered."*

This is not cosmetic, and it resolves a problem the earlier drafts left open.
Security review **F1** observed that ADR-0017 specified only a read API and never said
where credentials are *entered* — leaving two bad options: Game_shelf proxies
credential writes (so a UI app handles plaintext secrets for a service it does not
own), or the service grows a UI as an afterthought. Making the UI part of the service
from the start takes the third path: **the service owns its own admin surface, and no
consumer ever touches credential material.** It is also what makes the container
genuinely standalone — without it, "a single container that other systems connect to"
would still depend on Game_shelf's Settings page to be configured at all.

**Capabilities (the operator's list, made concrete):**

| Capability | Detail |
| --- | --- |
| **Add** a platform | Per-launcher credential form driven by that adapter's declared `auth_type` (api key / OAuth paste / cookie paste / password+OTP / file import). Submits to the admin plane (§5.5); write-only. |
| **Edit** | Re-submit credentials for an existing launcher — the routine action when an OAuth chain dies or a cookie expires. Never displays the stored value; shows metadata only (`configured`, `kind`, `updated_at`, `key_id`). |
| **Delete** | Removes credentials and disables the launcher. **Must NOT touch entitlement history** — see the trap below. |
| **View logs** | Per-launcher sync history from `sync_jobs`-equivalent records: started/finished, status, counts, and the **full error message**, which is the field that makes a failure actionable. Retention bounded (§9 OQ9). |
| **Status of all platforms** | The `/v1/health` classification (§5.4) rendered as a single board: `ok` / `failed` / `stale` / `attention_required` / `never_synced` / `locked` / `not_configured`, with last-success age and the attended action needed. |

**Attended workflows live here (§5.4).** This is where the operator pastes an emailed
Ubisoft OTP inside its 5-minute window, pastes a fresh Epic or GOG auth code, replaces
a Humble cookie, or uploads an Amazon/Ubisoft library export. An `attention_required`
state with nowhere to act on it is just a nag; the UI is what closes that loop.

**A trap the implementation must avoid, learned the hard way 2026-08-17.**
Game_shelf's `DELETE /api/launchers/:id/credentials` also runs
`UPDATE game_editions SET owned = 0 WHERE launcher_id = ?` — removing credentials
silently un-owns that launcher's whole library. Disabling EA and Humble via that
endpoint would have wiped **222 owned editions**. In this service, **"delete
credentials" and "forget what this account owns" are separate, separately-confirmed
actions**, and the destructive one states the row count before it runs.

**Security posture — the UI is the highest-value surface in the system.** It fronts a
store holding every launcher credential, so it inherits the admin plane's controls
(§5.5) and adds its own:

- Bound to the admin plane: LAN/loopback source allowlist **plus** a distinct admin
  credential. Not reachable with a consumer read token.
- Session auth with a real secret floor. Game_shelf's `GAMESHELF_JWT_SECRET` has a
  **minimum length of 1** (`server.js:6`) — that is the floor this service must not
  inherit. Minimum 32 chars, rejected at boot, fail-closed (the orchestrator's
  `orchestrator_token` validation is the model).
- Rate limiting and lockout on the login route — absent in Game_shelf (no `helmet`,
  no limiter in `package.json`), and the reason the security review rated its web
  tier below the orchestrator's.
- Credential submission is **write-only end to end**: no endpoint, and no view,
  re-displays a stored secret. This is what retires the plaintext-`totp_secret` QR
  endpoint (§5.5) rather than reproducing it.

**Scope honesty.** A management UI is real work — it is roughly the surface of
Game_shelf's Settings page plus a log viewer, and it is the largest single item in
Phase A. It is not optional: without it the service cannot be configured, and the
operator's requirement names it explicitly. Framework choice is deliberately left open
(§9 OQ10); server-rendered pages are sufficient and avoid a second build pipeline in
the container.

---

## 6. Migration sequence — every step independently valuable and reversible

| Step | What | Value on its own | Reversibility |
| --- | --- | --- | --- |
| 0 | ~~Partial-sync fix~~ **DONE, merged** (Game_shelf PRs #24–#26): adapters throw (`epic.js:142-155`, `humble.js:81-88`), unown-ratio guard (`syncEngine.js:142-153`), sync-health classifier + `GET /api/sync/health` | Silent mass un-owning already stopped, in production | n/a (shipped) |
| 1 | Crypto hardening in place (§5.6: KDF/raw key, AAD, key-id envelope, tested rotation) + remove the plaintext QR endpoint (§5.5) | Closes C7 where the credentials live today; unblocks every later step | Blobs re-encryptable both directions while both paths exist; endpoint removal loses nothing once `Setup.jsx` builds the URI from the submitted secret (**one** caller, `frontend/src/pages/Setup.jsx:207` — corrected 2026-08-19; the *zero-caller* claim is true of the TOTP generators per C1, not of the endpoint) |
| 2 | Cut the epicCatalog coupling inside Game_shelf (C3): catalog *facts* with sync, *presentation* consumer-side (OQ3) | Removes the hardest extraction blocker as a plain refactor, testable in place | Code revert |
| 3 | **Phase A service:** adapters + attended workflows + file imports (parser-isolated, §5.5) + contract (§5.2–5.4) + health; Game_shelf becomes consumer #1 | The operator's "single docker container / source of truth", live | Game_shelf's own sync engine remains deployable until step 6; flip back by config |
| 4 | Orchestrator consumer end (§5.9): ingest (reconcile pattern), `owned=0` + `last_sync_at` writers, staleness rejection, the three other enumeration-site fixes. **Runs in parallel with steps 2–3** — ingest is fed interim by a Game_shelf push if the service is not ready | **This is the step that fixes the motivating bug** | Config-flag off per sub-feature; `owned` flips are soft (never deletes cache, ADR-0015/0016); `--recently-purchased` stopgap remains as fallback |
| 5 | **Phase B broker** (§5.10): service performs ONE fresh Epic auth-code grant; orchestrator `_access_token` swaps to broker; delete `epic_session.json`; retire Game_shelf's epic credential blob | §2.4 duplication retired; one custodian, one re-auth | Orchestrator seam swaps back to file mode + one attended re-auth (the routine flow) |
| 6 | Credential handover for remaining launchers, **per launcher, last**: API keys (steam/xbox/itchio) copy trivially; OAuth launchers (gog) get a **fresh grant into the service** rather than a blob export; cookie/token launchers re-paste; files re-import | Single credential custodian; Game_shelf sheds its store | API keys trivially; others = one attended re-auth each, which is their routine flow anyway |
| 7 | **Steam badge custody (§5.11 v3)** — the service becomes custodian of SteamPrefill's and DepotDownloader's badges; the agent checks one out before a run and checks any renewal back in | Single vault: one place holding every credential, one expiry board, one re-auth surface | Restore the local files from the vault and disable checkout by config — no re-auth required |

**Why fresh grants, not blob export (steps 5–6):** Epic/GOG refresh tokens rotate on
use, so an encrypted blob copied at time T is dead the moment the old deployment
performs one refresh after T — and replaying a stale blob during rollback invalidates
the live session the same way. v1 specified a freeze-export-verify-disable protocol;
v2 replaces it where possible with the strictly simpler and safer **re-auth into the
service** (one attended paste — accepted by the requirement), reserving blob export
for nothing: API keys copy, files re-import, cookies re-paste. The freeze protocol
survives only as the fallback if a fresh grant is ever impossible.

---

## 7. Alternatives considered

### A. Incremental only, service trigger-gated (v1's decision) — NOT adopted in v2

Stated fairly: steps 0–4 alone deliver the motivating-bug fix, hardened credentials,
explicit ownership with revocation and freshness, and observable sync failures, with
zero new deployables. v1 adopted this and gated the service on triggers T1–T4. It
lost in v2 because it answers the wrong question: it optimizes "fix the bug with
minimum deployables" when the operator's requirement is the *service itself* (source
of truth + lists + any-system reads + possible download login). Two consumers with
distinct needs now exist (§4 point 2), which satisfies the letter of v1's own T1 as
applied to the restated requirement. What survives from A is its **sequencing
discipline**: the consumer-side work and in-place hardening proceed first and in
parallel, and no step waits on the service that does not need to.

### B. Shared library vendored into both apps

Rejected (unchanged from v1). A library cannot hold credentials once, cannot run a
schedule once, cannot host attended workflows — which is most of the value — and the
consumers are in two runtimes (Node adapters, Python orchestrator).

### C. Orchestrator-native Steam ownership client only (ADR-0016 Option B/D)

A direct `GetOwnedGames` client keeps the orchestrator standalone and is the shortest
path for the single platform that matters most. Not adopted as the *whole* answer: it
serves neither the lists, nor the other eight launchers, nor download auth, and it
still requires all of §5.9's consumer-side work. Remains the legitimate fallback if
the service (or the interim Game_shelf feed) proves unreliable — ADR-0016 Option D's
hybrid stays open.

### D. Build everything at once (Phase A+B+C, big-bang)

Rejected. Phase B must not precede the §5.6 crypto (Security > Speed). Steam badge
custody now *does* have a named need (§5.11 v3 — the operator's single-vault
requirement), but it still follows Phase A rather than running beside it, because it
depends on the service's store existing. Big-bang also couples the motivating-bug fix
(step 4) to the service's construction schedule for no benefit.

### E. Game_shelf hosts the whole requirement — no third deployable

The strongest cost-saving alternative and worth refuting precisely: Game_shelf's
backend already is "a single docker container that connects to the services and holds
the lists," and step 4's ingest could be fed by a Game_shelf push indefinitely.
Rejected on the broker role: token issuance (§5.10) inside the LAN-exposed UI app
concentrates *credential custody + token minting + browser-facing session auth* in
the process with the largest attack surface — the opposite of the trust boundary
§5.5 exists to draw, and contrary to priority 1. Rejected on lifecycle too: the
ownership/auth function should not go down whenever the UI app is redeployed (v1's
T4). If the operator ever de-scopes the broker entirely (OQ8), this alternative
regains force for the remainder — and should then be re-examined rather than
building Phase A out of momentum.

---

## 8. Consequences

- The motivating bug's fix stays decoupled from the new deployable: step 4 proceeds
  now, fed interim by Game_shelf if the service is not ready.
- A third deployable exists (Phase A onward): a new auth surface, per-consumer scoped
  tokens (§5.8), and the largest credential concentration on the LAN — accepted
  deliberately, with §5.5–5.7 as the standing mitigations and §5.5's parser isolation
  guarding the upload surface.
- Phase B makes the orchestrator's Epic prefill depend on the service's availability
  (fail-loud, retryable; OQ1). In exchange, the §2.4 duplication is retired: one Epic
  custodian, one re-auth paste restoring both consumers.
- Steam download auth **is custodied by the service** (§5.11 v3, decided
  2026-08-19). SteamPrefill keeps *running* on the prefill host, but its badge — and
  DepotDownloader's — live in the vault and are checked out per run. An earlier
  revision of this line asserted the operator "explicitly did not ask for one login
  for everything"; he subsequently asked for exactly that consolidation, and §5.11 v3
  is the answer: one custodian, distinct badges.
- Game_shelf ultimately sheds sync + credential custody (steps 3/6) and keeps
  UI/enrichment/identity — resolving v1's standing concern that it hosted a
  responsibility it "never asked to own".
- The ADR-0016 invariant holds: ownership is an explicit input; revocation becomes
  expressible end-to-end (generations → `owned=0` writer) for the first time; the
  616 + residue rows (ADR-0016 §1.4) become reconcilable at step 4.
- If nothing beyond step 0 is ever done: silent un-owning is already fixed, but the
  credential store keeps its C7 weaknesses and new-purchase coverage keeps riding the
  `--recently-purchased` stopgap. Step 1 is urgent independent of everything else.

---

## 9. Open questions

- **OQ1 — Service placement and broker coupling.** LXC (beside the control plane,
  natural for a control-plane service; broker call is LXC-local) vs NAS (beside the
  agent). Recommendation pending: LXC. Confirm the operator accepts Epic prefill
  failing loud when the service is down (§5.10) — the alternative (token caching in
  the consumer) reintroduces a second custody point by the back door.
- **OQ2 — Ubisoft password.** §5.5 defaults to session-artifacts-only with attended
  re-auth. Confirm the operator accepts occasional OTP+password re-entry over
  persistent password-in-blob storage (`stores_password: true` opt-in otherwise).
- **OQ3 — Epic post-processing cut line.** `nestDLC`/`resolveCodenames` write
  Game_shelf's tables and need a live Epic token (C3; invoked at
  `syncEngine.js:176-191`). Catalog *facts* travel with the sync; *presentation*
  stays consumer-side. Where exactly is the cut, and who calls the catalog API —
  the service (it has the token) or Game_shelf (it has the tables)?
- **OQ4 — Classification facts.** The orchestrator holds `steam_app_info`; Game_shelf
  holds `edition_tiers`. Which classification facts, if any, travel inside the
  contract versus remain consumer-local? Not needed for steps 0–4.
- **OQ5 — Entitlement transport.** Recommendation: **push** (service → orchestrator
  ingest, reusing the proven reconcile pattern; the orchestrator stays passive), with
  ingest internals transport-agnostic so pull is a config flip. Note the purity
  argument is weaker in Phase B — the orchestrator calls the service for tokens
  anyway — but push keeps Phase A working with the identical interim Game_shelf feed.
- **OQ6 — Steam selection feed.** Ownership-driven Steam prefill ultimately means
  writing `selectedAppsToPrefill.json` (host cron owns Steam, Piece 2). Does the
  reconcile/persist path (`scheduler/jobs.py:305-317`) become ownership-driven, or
  does the host cron's `--recently-purchased` remain the sole additive mechanism?
- **OQ7 — Epic sibling-chain behaviour (UNVERIFIED).** Two refresh chains from two
  separate auth-code grants against one account are live today (§2.4) and appear to
  coexist. Not verified: whether Epic enforces a per-account session cap that could
  someday evict one, or honours a rotation grace window. **No part of this design
  depends on the answer** — Phase B consolidates to one chain regardless — but the
  record must not silently upgrade "probably coexist" into fact.
- **OQ8 — Is Phase B wanted?** The requirement says "*possibly* be able to use it for
  logging in to download games." Phase B's honest benefit is custody consolidation
  plus one-fewer re-auth paste (§2.4, §5.10) at the cost of an availability coupling
  and a token-issuance surface. Severable either way (§4). Operator decision at the
  end of Phase A.

---

- **OQ10 — Management UI framework.** Server-rendered pages (no second build
  pipeline in the container, smallest dependency surface, adequate for an
  admin-plane tool used by one operator) vs a SPA matching Game_shelf's React
  stack (familiar, reusable components, but a second toolchain and bundle to
  maintain inside a credential-holding service). Recommendation pending:
  server-rendered. Either way §5.12's auth floor is binding.

## References

**Prior ADRs:** [ADR-0015] (purge is operator-driven, reversible; house format
reference) · [ADR-0016] (invariant; evidence; the counts in §1 C8) · ADR-0017
(rejected draft this replaces).

**Game_shelf** (`backend/src/`, origin/master @ 78875e6):
`services/launchers/epic.js:70-113,96-103,142-155` ·
`services/launchers/humble.js:16-24,81-88` ·
`services/syncEngine.js:5-12,53-57,66-78,122-165,142-153,174,176-191` ·
`services/syncHealth.js:17,32-80,88-92` · `routes/sync.js:6,43-66,69-108,94-97` ·
`routes/launchers.js:12-21,48,67,116,177` · `services/launchers/amazon.js:32-34` ·
`services/launchers/ubisoft.js:41-160,163-184,172-175,229-237` ·
`services/launchers/gog.js:64-88,79-84` · `services/launchers/steam.js:28-39` ·
`services/launchers/battlenet.js:21-24` · `utils/totp.js:8,46` ·
`services/launchers/epicCatalog.js:15,67` · `utils/encrypt.js:15-23` ·
`routes/setup.js:41-59` · `services/crossLauncherExclusions.js:45-59` ·
`server.js:92-94,102-108` · `services/orchestrator.js:6-68` ·
`services/manualCoverage.js:200-216` · `services/manualCoverageSnapshot.js:37-44`.

**lancache_orchestrator** (`src/orchestrator/`, main @ 7dae4fe):
`platform/epic/client.py:51-58,60-88,102-115` · `platform/epic/oauth.py:98-116` ·
`core/settings.py:86,88-93` · `jobs/handlers/prefill.py:131-132,323-326` ·
`api/routers/prefill_exclusions.py:69,140-200` · `jobs/handlers/library_sync.py:103` ·
`scheduler/jobs.py:155-175,305-317` · `platform/steam/manifest_fetcher.py:108-118` ·
`db/migrations/0001_initial.sql:16,35,38` · `api/main.py:184` · `agent/app.py:156`.

**Live-verified by the operator (2026-08-17/18):** both Epic chains active —
`/var/lib/orchestrator/epic_session.json` (mtime fresh) and Game_shelf's encrypted
`epic` credential row (last_sync fresh). See §2.4 for exactly what that does and does
not establish.

[ADR-0015]: 0015-operator-driven-cache-purge.md
[ADR-0016]: 0016-ownership-as-an-explicit-input.md
