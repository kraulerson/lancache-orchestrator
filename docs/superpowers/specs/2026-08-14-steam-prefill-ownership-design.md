# Design — orchestrator ownership of scheduled Steam prefill

**Date:** 2026-08-14
**Status:** Draft (design) — awaiting Orchestrator approval
**Repo:** lancache_orchestrator. **Branch:** `feat/steam-prefill-ownership` (not yet created)
**Parent:** re-arch ① (`docs/superpowers/specs/2026-06-19-steam-via-prefill-design.md`), which delegated Steam *execution* to SteamPrefill. This spec proposes moving Steam *scheduling* from the host cron to the orchestrator.

> **Not in scope — settled and explicitly retained.** SteamPrefill remains the Steam engine. The orchestrator does not re-implement Steam auth, CDN, or manifest fetching. ① adopted SteamPrefill precisely because the ValvePython worker rode Steam's deprecated legacy auth and produced the mass-`NotAuthenticated` cascade during a 2,484-game prefill sweep. Nothing here reopens that.

---

## 1. Problem

Steam prefill is driven by a host cron (`0 */6 * * * run-steam-prefill.sh`) that the orchestrator neither triggers nor observes. Epic is orchestrator-owned. The split has three concrete costs:

1. **The orchestrator's Steam data is misleading.** Host-cron prefills write no `jobs` row and never touch `games.last_prefilled_at`. On 2026-08-14 the DB reported the last Steam prefill as 2026-08-09 while SteamPrefill had in fact run successfully five times in the preceding 26 hours. Any operator — or any future automation — reading the DB to answer "is Steam prefill healthy?" gets a wrong answer.
2. **Two alerting systems.** Steam stalls email via `run-steam-prefill.sh` → `cache-catcher`; everything else surfaces through `/health`, job state, and the orchestrator's logs. Neither knows about the other.
3. **Two writers to one selection file.** `selectedAppsToPrefill.json` is written by the orchestrator (Piece 1 prune, `reconcile_selection`) *and* consumed by the host cron, with `SteamPrefillDriver.prefill_apps` also temporarily overwriting and restoring it. Nothing coordinates them.

## 2. What already exists (do not rebuild)

- **`SteamPrefillDriver.prefill_apps(app_ids, force)`** — writes `selectedAppsToPrefill.json`, runs the binary with `--no-ansi`, restores the operator's prior selection in a `finally`. Already used by on-demand prefill (CLI, Game_shelf Repair).
- **Agent endpoints** — `POST /v1/steam/prefill` (async job id) + `GET /v1/steam/prefill/{job_id}`, plus `downloaded-state`, `auth-status`, `prune-selection`, `prefilled-apps`.
- **`enqueue_scheduled_prefill`** — the wall-clock-cron scheduled driver, currently `platform = 'epic'` only, honouring `block_list` and `prefill_exclusions`.
- **Manifest capture after prefill** — `sync_manifests_to_archive` on the agent, guarding the false-Partial root cause.

The migration is therefore mostly **wiring plus hardening**, not new capability.

## 3. Findings that gate this work

### 3.1 BLOCKER — the orchestrator path is less safe than the cron it would replace

`prefill_apps` calls `await proc.communicate()` with **no timeout**, and `POST /v1/steam/prefill` spawns `_run()` with **no mutual exclusion**.

| Protection | Host cron | Orchestrator path |
|---|---|---|
| Overlap prevention | `flock -n` on `steamprefill.lock` | none |
| Stall detection | consecutive-skip counter, email at 3 (~18h) | none |
| Timeout | `timeout -k 60 10h` | none on the subprocess; `AgentClient.poll_timeout_sec=7200` abandons the *poll* while the process keeps running |

This matters because of a recorded incident: on 2026-08-12 a `SteamPrefill --force` was found alive for **5 days 18 hours**, having already logged `Prefill complete!` and then never exited. `run-steam-prefill.sh` exists specifically to make that loud.

Under orchestrator ownership as the code stands today, the next scheduled tick would start a **second concurrent SteamPrefill**. ① §6 states SteamPrefill "isn't built for concurrent invocations sharing one auth/cache". The cron's `flock` is currently the only thing preventing that.

**Consequence: hardening is a prerequisite, not a follow-up.**

### 3.2 The `--recently-purchased` premise is stale

`scheduler/jobs.py` and ① both assert the host cron "auto-grabs recent purchases". It does not, and has not since the v4 cron rewrite (2026-08-12):

```
$ crontab -l | grep -i recently-purchased
(no matches)
$ grep 'SteamPrefill prefill' run-steam-prefill.sh
  'cd /SteamPrefill && HOME=/tmp ./SteamPrefill prefill --no-ansi'
```

New games still reach `up_to_date` (verified: Batman: Arkham Knight, Gears 5, Sir We Have an Orc Problem), but through a different mix: the selection list, the gated sweep validating `unknown` rows (#250 Piece 1), and lancache caching organically whatever is actually downloaded through it.

**Consequence:** blocker "must replicate `--recently-purchased`" is smaller than assumed — the cron is not providing that today. It should be an explicit decision (§7 OQ1), not an inherited assumption. The stale comments must be corrected regardless of whether the migration proceeds.

## 4. Architecture

Four phases, each independently valuable and independently revertable. **Phases 1–2 are worth shipping even if 3–4 are abandoned.**

```
Phase 1  harden the driver        -> safe to invoke unattended
Phase 2  operational parity       -> stalls are as loud as they are today
Phase 3  scheduled Steam prefill  -> orchestrator drives it, cron still present
Phase 4  retire the host cron     -> exactly one writer
```

### Phase 1 — driver hardening

`SteamPrefillDriver.prefill_apps`:
- Wrap `communicate()` in `asyncio.wait_for` with a configurable `steam_prefill_timeout_sec` (default **36000** = 10h, matching today's `RUN_MAX`).
- On timeout, terminate the process **group** (`start_new_session=True` + `os.killpg`), escalating `SIGTERM` → 60s → `SIGKILL`. This is strictly better than the cron, whose `timeout` kills only the `docker exec` client and leaves the in-container process alive — a limitation its own alert text admits.
- Always restore the prior selection file (the existing `finally` already does; add a test that it survives the timeout path).

Agent `POST /v1/steam/prefill`:
- Add an **asyncio lock or single-flight guard** so a second prefill request while one is running returns the in-flight `job_id` (dedup) rather than launching a concurrent SteamPrefill. This mirrors the orchestrator's existing prefill-trigger dedup semantics.

### Phase 2 — operational parity

Everything `run-steam-prefill.sh` does, expressed in orchestrator terms:

| Cron behaviour | Orchestrator equivalent |
|---|---|
| `flock -n` skip | Phase 1 single-flight → job row `state='queued'` deduped onto the in-flight run |
| skip counter + email at 3 | a `steam_prefill_stall` check: a `prefill` job `running` beyond `steam_prefill_timeout_sec`, or N consecutive dedup-skips, raises an alert |
| `RUN_MAX` timeout alert | Phase 1 timeout → job `state='failed'`, `error='timeout after Ns'` |
| email via cache-catcher | **OQ2** — reuse the cache-catcher SMTP path, or add a first-class orchestrator alert channel |

A job row with `started_at` set and `finished_at` NULL past the timeout is directly queryable — strictly more observable than a skip counter in a file. The existing startup job reaper (ID6) already handles orphaned `running` rows across restarts.

### Phase 3 — scheduled Steam prefill

Extend `enqueue_scheduled_prefill` to cover Steam, or add a sibling `enqueue_scheduled_steam_prefill`. **Decision (OQ3):** a sibling is preferred — the Epic predicate keys off validation status (`status <> 'up_to_date'`), which for Steam would enqueue all 1,363 `not_downloaded` rows, i.e. games deliberately outside the selection. Steam's candidate set is *the selection list*, not the owned library.

Schedule: wall-clock `CronTrigger`, following the pattern merged in PR #273. Slot must avoid the Steam-heavy windows already in play — proposal `15 1,7,13,19 * * *` UTC, i.e. 1h15m after the current cron slots and well clear of both the validation sweep (`0 3,9,15,21`) and Epic prefill (`45 3,9,15,21`).

### Phase 4 — retire the host cron

Only after Phases 1–3 run green live for a full week. Remove the `0 */6` line from `prefill-cron-v4` **and** the live spool file (note: `crontab <file>` cannot install on this NAS — the spool dir is root-owned; write the spool file directly, as of 2026-08-14). Keep `run-steam-prefill.sh` on disk one release cycle as the rollback path.

## 5. Security

- No new credential surface. The orchestrator still never holds Steam credentials; SteamPrefill owns `account.config`. The driver reads only the token's `exp`.
- `selectedAppsToPrefill.json` writes stay app-id-ints-only, validated by `_validate_app_ids` before write (unchanged).
- Killing a process group is the one new privileged-ish operation; it is scoped to the child the agent itself spawned.

## 6. Testing

- **Phase 1:** a fake binary that sleeps past the timeout asserts (a) the job fails with a timeout error, (b) the process group is dead, (c) the prior selection file is restored. A second concurrent request asserts the in-flight `job_id` is returned and only one process was spawned.
- **Phase 2:** stall detection asserted against a synthetic `running` job older than the timeout.
- **Phase 3:** trigger type/slot assertions mirroring `tests/scheduler/test_manager.py::TestScheduledPrefillRegistration`; candidate-set SQL asserted to select from the selection list, **not** all owned Steam rows.
- **Phase 4:** live-only. Gate below.

## 7. Open questions

- **OQ1 — new-purchase discovery.** Restore `--recently-purchased` (as an orchestrator-driven mode on the driver), or formally accept the current mechanism (selection list + organic lancache caching + sweep validating `unknown`) and correct the stale comments? *Recommendation: restore it as an explicit driver mode; it is the only mechanism that is deliberate rather than incidental.*
- **OQ2 — alert channel.** Reuse `cache-catcher`'s SMTP via `docker exec`, or give the orchestrator a first-class notifier? The former is expedient and already configured; the latter removes a cross-container dependency from the alert path.
- **OQ3 — one job or two.** Confirmed above as two (Steam's candidate set differs fundamentally from Epic's). Recorded here for explicit sign-off.
- **OQ4 — `--force` scheduling.** The 5d18h hang was a `--force` run. Should scheduled Steam prefill ever pass `--force`, or remain operator-only? *Recommendation: operator-only.*

## 8. Go / no-go gate before Phase 4

All must hold for 7 consecutive days:

1. Every scheduled Steam prefill produced a terminal `jobs` row (no orphans).
2. At least one deliberate overlap was deduped, not double-run.
3. A deliberately induced stall raised an alert.
4. `games.last_prefilled_at` for Steam tracks actual runs.
5. Coverage did not regress: Steam `up_to_date` count stable or better (baseline 2026-08-14: **1,138**).

Rollback at any point: re-add the cron line, disable the orchestrator's Steam schedule via settings. No data migration, so rollback is configuration-only.
