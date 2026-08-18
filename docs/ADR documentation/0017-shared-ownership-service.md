# ADR-0017: A Shared Ownership Service

<!-- Last Updated: 2026-08-17 -->

> ## ⚠ REJECTED — DO NOT BUILD FROM THIS DOCUMENT
>
> **Status: Rejected — 2026-08-17.** Two independent adversarial reviews
> (architecture and security) verified this ADR's claims against the code and
> found its load-bearing evidence false on **both** sides of the decision — the
> structural benefit *and* the extraction cost. Superseded by
> **[ADR-0018](0018-standalone-ownership-service.md)**, which re-derives the
> decision from verified code and carries a full corrections register in its §1.
>
> It is retained, unedited below this banner, because ADR-0018 cites it and
> because a rejected design is worth recording — it stops the same proposal
> being re-made. **Its factual claims are not reliable.** The headline errors:
>
> | This ADR claims | Reality |
> |---|---|
> | Battle.net stores `password` + `totp_secret` — "the single largest new risk" | Battle.net is an unimplemented **stub**; the app refuses to store its credentials. **Ubisoft** is the password holder |
> | "Headless capability" — 2FA generated from a stored secret | `generateTOTPCode` has **zero** production callers; Ubisoft's 2FA is an **emailed** code. ~5 of 10 launchers need a human |
> | "Launcher → DB coupling: **None**" | `epicCatalog.js` takes `db` as a positional parameter and writes Game_shelf's schema — the grep used could not see it |
> | "Already a neutral DTO" | ~7 fields named after `game_editions` **columns** |
> | §1: neither pull nor push works today | Game_shelf **already** pushes *and* pulls in production, with graceful degradation already implemented |
> | §3's classification divergence | The stated figures sum to 1363 of 2508; the systems actually agree within ~2% |
>
> It also would not have fixed the motivating bug: the orchestrator has **four**
> cache-derived enumeration sites, and this ADR addressed one.

**Original text follows, unedited.**

---

**Status:** ~~Proposed~~ **REJECTED** — 2026-08-17. **Supersedes [ADR-0016]** (which proposed a
Game_shelf → orchestrator push). Raised by Karl: extract Game_shelf's
launcher-authentication and library-sync capability into a service that both
Game_shelf and lancache_orchestrator consume.

This ADR spans two repositories (`lancache_orchestrator`, `Game_shelf`) and
would introduce a third.

---

## 1. Why this supersedes ADR-0016

ADR-0016 established the right invariant — **ownership is an explicit input,
never inferred from cache contents** — but its mechanism was weak. It offered
two shapes, and both are worse than a shared upstream:

| Shape | Problem |
| --- | --- |
| Orchestrator **pulls** Game_shelf | Circular dependency between two separately-deployed services (LXC 1102 ↔ 1105); each becomes a hard availability dependency of the other |
| Game_shelf **pushes** to orchestrator | Makes a library/UI app double as a data provider for an unrelated system; ownership sync becomes a Game_shelf feature it never asked to own |

A shared service below both means **neither app depends on the other**. That is
a structural improvement, not a cosmetic one, and it is the strongest argument
for this proposal — stronger than the code-reuse argument.

## 2. Evidence — the capability is already extraction-shaped

Measured against `Game_shelf@03cd89f`, not assumed:

| Property | Finding |
| --- | --- |
| Launcher → DB coupling | **None.** `grep -c 'this\.db'` is 0 across all 13 launcher modules; only `base.js` names it, in a constructor parameter nobody uses |
| Contract | Uniform: `authenticate(credentials)`, `fetchOwnedGames(session)`, `refreshIfNeeded(credentials)` |
| Output | Already a neutral DTO — `{launcher_game_id, title, playtime_minutes}` — not Game_shelf's schema |
| Size | 1,217 lines across 13 modules (`steam.js` is 51) |
| Credentials at rest | AES-256-GCM, key from `GAMESHELF_ENCRYPTION_KEY`, **fails closed** if absent or <32 chars |
| Headless capability | `syncAll(db)` runs on a scheduler; Battle.net stores `totp_secret`, so 2FA is generated, not prompted. The `otp_code` route parameter is a manual override only |

Extraction is close to lift-and-shift. This materially lowers the cost side of
the decision.

## 3. The correction to the original framing

Karl's proposal was "pull everything, filter nothing; let each consumer filter."
The first half is right; the second half, taken literally, is the expensive part.

If the service ships raw records with no classification, **both consumers must
classify independently, and they will diverge.** This is demonstrated, not
hypothetical: on 2026-08-17 the orchestrator held 2508 Steam rows it treated as
games; the true composition was 684 dlc, 616 never-classified, 41 game, 11
advertising, 9 demo, 2 music — while Game_shelf's own Steam call returned 1115.
Two classifiers over one account, two different answers to "do I own this game."

**Resolution — the service is unopinionated about *inclusion* and authoritative
about *facts*.** It filters nothing and stores everything the storefront returns,
and it additionally carries classification metadata (`kind`, `is_dlc`,
`is_tool`, `categories`, `parent_app_id`, …). Consumers then filter from shared
truth rather than each inventing it. Karl agreed to this correction on
2026-08-17.

## 4. Design

### 4.1 Boundary

A standalone service ("the ownership service") that owns, for each configured
launcher: credentials, authentication and token refresh, scheduled polling, and
a durable record of everything the storefront reported. It owns **no** notion of
"prefillable", "displayable", "cached", or "wanted". Those are consumer
concerns.

Implementation language is **Node**, so the existing 13 adapters lift verbatim.
The HTTP boundary means the orchestrator (Python) never observes this.

### 4.2 API (read side)

- `GET /v1/launchers` — configured launchers with per-launcher auth + sync health
- `GET /v1/entitlements?launcher=&since=` — every record, unfiltered, with
  classification metadata and a monotonic cursor for incremental pulls
- `GET /v1/health` — per-launcher `last_success_at`, `last_error`, `stale`

Consumers **pull**. The service pushes to nobody, so adding a consumer requires
no service change.

### 4.3 Freshness is first-class

Every response carries per-launcher `last_success_at` and a `stale` flag.
Consumers must **reject or loudly flag** a stale launcher rather than treat an
empty or shrunken list as truth. This is the direct lesson of the failure that
motivated ADR-0016: at the time of writing, Game_shelf's `ea` and `humble` syncs
are failing and `ubisoft`/`xbox` last succeeded 2026-04-07 — and nothing
surfaces it. A naive "replace my library with what was returned" would silently
delete real coverage.

### 4.4 Consumers keep working when it is down

Each consumer caches last-known-good entitlements and degrades rather than
fails. The orchestrator must not lose the ability to validate and prefill
because an ownership service is unreachable — today its Epic sync is entirely
independent of Game_shelf, and centralising must not make its availability
strictly worse.

## 5. Security — the part that grows

Concentrating nine storefronts' credentials in one service is a **materially
higher-value target** than either app today. Specifically, Battle.net stores
`password` *and* `totp_secret` together, so a compromise is full account
takeover **with 2FA bypassed**. This is the single largest new risk and must be
designed, not inherited:

- Encryption at rest carries over (AES-256-GCM, fail-closed key check), but the
  key is one environment variable — blast radius grows with the credential count
- The service should hold the **least-privileged credential each launcher
  supports** (Steam needs only a Web API key + SteamID64 — no password, per
  `steam.js`); password-based launchers are the exception, not the pattern
- Consumers get read-only entitlement access and **never** see credentials
- Credential rotation is already in the flow (`refreshIfNeeded` returns
  `updatedCredentials`, re-encrypted and persisted) and must move with it

## 6. Sequencing — do not build it before something consumes it

Only **one** consumer currently needs data it cannot already get: the
orchestrator, for Steam. Epic already works there. Building a shared service for
a one-consumer need is speculative generality.

The counter is genuine: eight launchers have no orchestrator-side integration at
all, and building those twice is worse than building the service once. So the
destination is right; the order matters.

1. **Extract, don't rewrite.** Move the launcher adapters into the service
   as-is, with Game_shelf as its first consumer. No behaviour change; Game_shelf
   should be unable to tell the difference except that sync health is now
   visible.
2. **Add classification metadata**, since only the service can do it once.
3. **Point the orchestrator's Steam library_sync at it**, replacing the
   circular `prefilled_apps()` enumeration (ADR-0016 §1).
4. **Reconcile and retire** the 1363 licence-residue rows (ADR-0016 §1.4).
5. Extend to other launchers as prefill support for them actually appears.

## 7. Consequences

- A third deployable: to run, monitor, back up, and secure.
- Game_shelf loses ~1,200 lines and gains an HTTP dependency.
- The orchestrator's `library_sync` Steam path becomes a consumer of an explicit
  ownership source; the comment corrected in `scheduler/jobs.py` and the
  invariant in ADR-0016 §3 both hold unchanged.
- Sync failures become observable for the first time.
- If nothing is done: Option A of ADR-0016 (host cron `--recently-purchased`,
  already deployed) keeps new Steam purchases visible, so the acute symptom
  stays fixed and this remains a structural improvement rather than an outage
  fix.

## 8. Open questions

- **OQ1 — Where does it run?** A third LXC, or alongside one of the existing
  services? Co-locating undercuts the availability argument.
- **OQ2 — Does Game_shelf keep a local cache, or hard-depend on the service?**
  §4.4 argues for a cache; that is a Game_shelf change beyond pure extraction.
- **OQ3 — Who owns classification per launcher?** Steam has `appdetails.type`;
  most others have nothing comparable. Is `kind` best-effort and nullable?
- **OQ4 — Migration of existing credentials.** Re-enter by hand, or move the
  encrypted blobs with the key? The latter is faster and keeps the key's blast
  radius unchanged.
- **OQ5 — Is a service warranted at all versus a shared library** vendored into
  both apps? A library avoids the third deployable but cannot hold credentials
  or a schedule once, which is most of the value.

---

## References

- [ADR-0016] — ownership as an explicit input (superseded by this ADR; its
  invariant is retained)
- `Game_shelf@03cd89f` — `backend/src/services/launchers/*`,
  `services/syncEngine.js`, `utils/encrypt.js`
- `jobs/handlers/library_sync.py:89` — the circular enumeration this replaces
