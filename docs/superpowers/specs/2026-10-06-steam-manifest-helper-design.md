# Steam Manifest Helper — Design

**Date:** 2026-10-06
**Status:** Draft — awaiting Orchestrator review
**Issue:** #361 (UAT 17 triage: Fix Now)
**Supersedes:** the one-DepotDownloader-process-per-app fetch from #213, and the
#228 retry/back-off that was meant to make it safe

## Problem

Every Monday, `fetch_manifests` holds the Steam account in a login rate limit for
about four hours. On 2026-10-06, job 46828 reported `apps=1211 fetched=82
skipped=1353 failed=729`. The 06:00 MDT SteamPrefill run, which shares the account,
failed with `RateLimitExceeded` on 2026-09-22, 09-29 and 10-06. That delays new
purchases by six hours and turns Kuma 176 red every week.

The job does necessary work. SteamPrefill saves a manifest only when it downloads
something, so this job is the only way the system learns that many games have
changed: of the 82 new depot manifests on 2026-10-06, **25** had no other source.
It just pays for those 25 with 1,211 Steam logins.

## Root cause (verified 2026-10-06)

1. **One Steam login per game.** `DepotDownloaderManifestFetcher.fetch_all()` runs a
   separate DepotDownloader process per app, and each logs in from scratch.
   DepotDownloader's command line takes a single `-app`. Run on the NAS:
   `Logging '<user>' into Steam3... Done!` on every invocation.
2. **The #228 back-off has never run.** It classifies a failure as transient by
   searching `proc.stderr` (`manifest_fetcher.py:226`). DepotDownloader writes
   everything to **stdout**: its docs say errors go via `Console.WriteLine`, and a
   live run on the NAS for a non-existent app gave `returncode: 1`, 291 stdout
   characters, 0 stderr characters. On 2026-10-06 that meant 729 `dd_nonzero`
   events, all with `stderr: ""`, and **0** `manifest_fetch.transient_retry`
   events. Every refusal was filed as permanent, never backed off, and its
   reason was never logged.
3. **Timing (contributing).** Since 2026-09-22 the Monday 05:00 UTC job waits
   behind the 03:00 UTC sweep, which runs to its ~5.5 h deadline. That moved the
   login storm across the 06:00 MDT SteamPrefill tick, about 3.5 h into the run.
   Every tick about 3 h or more into a run was refused (4 of 4); ticks about 1 h
   in were refused 1 time in 4.

Fixing cause 2 alone would make things worse. Each refused app would retry three
times with 15/30/60 s waits, stretching a ~4 h run to roughly a day (an estimate)
while every sweep waits behind it.

## Decisions (Karl, 2026-10-06)

| Question | Decision |
|---|---|
| Fix approach | **A helper that logs in once**, chosen over containing the storm first |
| Built from | **Fresh code on SteamKit2** (LGPL-2.1), so the helper stays MIT. DepotDownloader is GPL-2.0 |
| Login | **Import the token DepotDownloader already saved**; no phone approval |
| Split of work | **Thin helper.** Python keeps choosing games, parsing manifests and writing `.shas` |
| Old path | **Replace DepotDownloader; roll back by image tag.** Revised from "keep it switchable" once CI's 250 MB image limit showed both cannot fit (see Packaging) |

## Design

### 1. Components and interface

**The helper** (`tools/steam_manifest_helper/`, C#, .NET, SteamKit2) is a
command-line program with two subcommands.

`fetch` runs once per weekly job:

```
steam-manifest-helper fetch --apps <file> --out <dir> --session-dir <dir> [--import-from <dir>]
```

1. **Load the session.** Read the token from `<session-dir>/session.json`. If
   that file is absent, import it once from DepotDownloader's store under
   `--import-from` (the `.local/share/IsolatedStorage/**/account.config` path the
   current fetcher already globs: a Deflate stream holding a protobuf
   `AccountSettingsStore`, whose field 4 `LoginTokens` maps username to token).
   Write `session.json` with mode 0600.
2. **Log in once** with that token.
3. **Ask for every app's info** (PICS product info) in batches.
4. **Choose depots** with the same rules as DepotDownloader 3.4.0's
   `-os windows -osarch 64` (`oslist`/`osarch` checks, `ContentDownloader.cs:482-494`).
5. **Fetch each depot's manifest** with `CDNClient.DownloadManifestAsync`, and
   save it as `<out>/<app>/<depot>_<gid>.manifest` with `DepotManifest.SaveToFile`.
   These are the same SteamKit2 calls DepotDownloader makes (`ContentDownloader.cs:796`,
   `Util.cs:197-198`), so Python's existing `parse_steamkit_manifest` reads them
   unchanged.
6. **Report.** stdout carries one JSON object per app, then one summary line:

```
{"app": 730, "status": "ok", "manifests": ["731_7617088375292372759.manifest"]}
{"app": 999, "status": "not_owned|no_depots|error|not_attempted", "reason": "<Steam's own text>"}
{"summary": true, "session": "completed|login_refused|import_failed|disconnected", "reason": "..."}
```

stderr carries a human-readable log. Exit codes: 0 = session completed (per-app
failures possible); 2 = login refused; 3 = session import failed; 4 = disconnected
twice; 1 = anything unexpected.

`login` is a one-time interactive login: username, password, and Steam Guard
approval on the phone. It writes `session.json`. It is needed when the imported
token is rejected or eventually expires, because once DepotDownloader is removed
nothing else can renew the session.

**The Python job keeps its role.**
- `fetch_all()` keeps its signature and `FetchResult`, so the agent API and the
  orchestrator see no interface change.
- It still enumerates the apps (`_enumerate_app_ids`, unchanged).
- It writes them to a file and runs the helper **once**, in place of the per-app
  `_run_manifest_only`/`_run_with_retry` loop.
- It parses each `ok` app's manifests with the existing parser, writes `.shas` with
  the existing `_write_shas`, and counts fetched/skipped/failed as today.

Settings: `depotdownloader_binary` is replaced by `steam_manifest_helper_binary`.
`manifest_fetch_delay_sec`, `manifest_fetch_max_retries` and
`manifest_fetch_retry_backoff_sec` are removed; the live agent sets none of them.
`depotdownloader_config_dir` stays, because it is where the session lives.

### 2. Failure handling

| Situation | Behaviour |
|---|---|
| Steam refuses the login | One attempt only, never a loop. Exit 2. The job fails with Steam's text in its reason |
| Token import fails | Exit 3. The job fails, and the reason names the `login` command |
| Connection drops mid-run | Wait 60 s, reconnect **once**, continue from the next app. A second drop or a refused reconnect: stop, mark the rest `not_attempted`, exit 4 |
| One app fails (not owned, no Windows depots, CDN error) | Counted as failed with its reason logged; the run continues |
| Helper hangs | Python kills it after 2 h; the job fails |
| Session stopped early (exit 2, 3 or 4) | `.shas` are still written for every `ok` app, then `fetch_all` raises with fetched/failed/not-attempted counts and the reason |
| Nothing fetched or skipped, but failures | Unchanged: `fetch_all` raises, as today |

Python reads **both** streams. On any failure it logs the summary reason and the
tail of stderr. The token is never printed or logged by either side.

### 3. Packaging

- **Source:** `tools/steam_manifest_helper/` holds the program and an xUnit test
  project. NuGet versions are pinned exactly, with a committed `packages.lock.json`.
- **Build:** a new Dockerfile stage `FROM --platform=$BUILDPLATFORM
  mcr.microsoft.com/dotnet/sdk`, pinned to an exact digest when the plan is
  implemented, as the Python base image is.
  - Runs `dotnet test`: a failing test fails the image build.
  - Publishes self-contained for the target architecture, cross-compiled, so
    CI's arm64 build never runs the compiler under QEMU.
  - The output goes to `/steam-manifest-helper/` in the runtime stage.
  - The DepotDownloader download stage is removed.
  - CI needs no workflow change.
- **Size:** CI fails any amd64 image over 250 MB. The image is 255 MB in decimal
  units (about 243 in CI's), and DepotDownloader is 76 MB of it. If the helper
  build exceeds the limit, stop and bring it back to Karl. No silent trimming.
- **Licensing:** the helper's own code is MIT.
  - SteamKit2 (LGPL-2.1) and its dependencies are published as separate,
    replaceable assemblies, not single-file. That is what the LGPL asks of
    programs that use the library.
  - A new `THIRD_PARTY_NOTICES.md` lists every bundled component and its license.
- **Session storage:** `/depotdownloader-config/steam-manifest-helper/session.json`,
  on the existing persistent mount. No new volume or compose change.
- **Rollback:** the deploy convention already tags the previous image
  `orchestrator:dpa-pre-<N>` on both hosts. To go back:
  `docker tag orchestrator:dpa-pre-361 orchestrator:dpa`, then recreate the agent in
  an inter-sweep gap. No rebuild.

### 4. Testing and live verification

**Helper (xUnit, test-first):**
- Token import from a synthetic Deflate+protobuf `account.config` built in the
  test. A missing or corrupt store fails with exit 3. The token appears in no
  output.
- Depot selection matches DepotDownloader's `oslist`/`osarch` rules on fixture
  app info.
- Session policy, against a fake Steam connection behind an interface: one login
  attempt; at most one reconnect; a second drop gives `not_attempted` plus exit 4;
  Steam's refusal text reaches the JSON reason.

**Python (pytest, test-first, with a scripted stand-in helper):**
- One helper call per run, carrying the full app list.
- Manifest fixtures produce the same `.shas` as today.
- Exits 2, 3 and 4 raise with the reason and the counts.
- A hang is killed (tiny timeout in the test).
- `.shas` are written for `ok` apps even when the session stopped early.

**Parity, the correctness gate:** for apps DepotDownloader already fetched, the
helper's `.shas` must be **byte-identical** to those already in
`/manifest-archive/v1`. Checked in the spike, and again on the first real run.

**Spike (plan task 1; throwaway; on the NAS, in a quiet window):** import the
token, then fetch about 100 apps in one session. It answers four questions:
1. Does the import and login work?
2. Does Steam limit manifest requests within a single session?
3. Parity: are the `.shas` byte-identical?
4. What does the image size come to?

**First real run:** started by hand with `orchestrator-cli cache fetch-manifests`,
in a gap between sweeps.

## Success criteria (from #361)

- `fetch_manifests.done` shows `failed` near 0, with each remaining failure's
  reason logged.
- The helper's log shows exactly one Steam login per run.
- The following Monday's 06:00 MDT SteamPrefill run ends `END steam prefill ok`,
  with no `RateLimitExceeded`.
- Kuma 176 stays UP.
- Sweeps keep succeeding.

#361 closes only on that Monday evidence.

## Out of scope

- Moving the Monday schedule, or letting the job jump the sweep queue. Once the
  storm is gone, the overlap is harmless.
- Distinguishing "not owned" from other per-app failures in `FetchResult`. The
  reason is logged; changing the interface is not needed.
- #330, #331 and the other open issues.

## Risks

- **Token import is unproven** until the spike. If it fails, fall back to `login`
  (needs Karl's phone, once).
- **Steam may limit manifest requests inside one session.** That is unproven
  either way. If the spike sees it, pace the requests inside the session (which
  costs no logins), and revisit before building on.
- **Token lifetime:** Steam tokens expire. Expiry surfaces as a loud exit 3/2
  failure that names the `login` command, never as a silent success.
- **Image size** may not fit under 250 MB. Measured in the spike; Karl decides if
  it does not.
