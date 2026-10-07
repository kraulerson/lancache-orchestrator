# ADR-0019: A One-Login Steam Manifest Helper

<!-- Last Updated: 2026-10-07 -->

**Status:** Accepted — 2026-10-07 (Orchestrator: Karl Raulerson). Issue #361.
Design: `docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md`.
Plan: `docs/superpowers/plans/2026-10-07-steam-manifest-helper.md`.
Supersedes the one-DepotDownloader-process-per-app fetch from #213, and the #228
retry/back-off that was meant to make it safe. Like [ADR-0013] (itself
superseded), it keeps Steam code in a separate child process, not in the API
process.

---

## 1. Context

Every Monday `fetch_manifests` held the Steam account in a login rate limit for
about four hours. On 2026-10-06, job 46828 reported
`apps=1211 fetched=82 skipped=1353 failed=729`. The 06:00 MDT SteamPrefill run,
which shares the account, failed with `RateLimitExceeded` on 2026-09-22, 09-29
and 10-06, delaying new purchases by six hours and turning Kuma 176 red every
week. The job is necessary: SteamPrefill saves a manifest only when it downloads
something, so this job is the only way the system learns that many games changed
(25 of the 82 new depot manifests on 2026-10-06 had no other source).

Root cause, verified 2026-10-06:

1. **One Steam login per game.** `fetch_all()` ran a separate DepotDownloader
   process per app (DepotDownloader's command line takes one `-app`), and each
   logged in from scratch: 1,211 logins in a run.
2. **The #228 back-off never ran.** It classified a failure as transient by
   searching `proc.stderr`, but DepotDownloader writes everything to **stdout**.
   On 2026-10-06 that produced 729 `dd_nonzero` events, all with `stderr: ""`, and
   **0** `manifest_fetch.transient_retry` events. Every refusal was filed as
   permanent, never backed off, and its reason was never logged.
3. **Timing (contributing).** Since 2026-09-22 the Monday job waits behind the
   03:00 UTC sweep, moving the login storm across the 06:00 MDT SteamPrefill tick.

Fixing cause 2 alone would have made things worse: three retries per refused app
would stretch a ~4 h run toward a day while every sweep waits behind it.

## 2. Decision

Replace the per-app DepotDownloader loop with **one helper process that logs in
once** and fetches every app's manifests in that session. The decisions, from the
spec (Karl, 2026-10-06):

| Question | Decision |
|---|---|
| Fix approach | **A helper that logs in once**, chosen over containing the storm first |
| Built from | **Fresh code on SteamKit2** (LGPL-2.1), so the helper stays MIT. DepotDownloader is GPL-2.0 |
| Login | **Import the token DepotDownloader already saved**; no phone approval |
| Split of work | **Thin helper.** Python keeps choosing games, parsing manifests and writing `.shas` |
| Old path | **Replace DepotDownloader; roll back by image tag.** Revised from "keep it switchable" once CI's image limit showed both cannot fit |

Shape of the result:

- `tools/steam_manifest_helper/` is a C# (.NET 10) program with `fetch` and
  `login` subcommands. `fetch` takes `--apps`, `--out`, `--session-dir`,
  `--username` and `--import-from`. stdout carries JSON lines only (one per app,
  then a summary); stderr is the human log. Exit codes: 0 completed, 1
  unexpected, 2 login refused, 3 session import failed, 4 disconnected twice, 64
  usage.
- Exactly one Steam logon per run. After a dropped connection or a Steam log-off,
  wait 60 s and reconnect **once**; a second loss ends the run with the rest
  `not_attempted` and exit 4.
- `fetch_all()` keeps its signature and `FetchResult`, so the agent API does not
  change. Python runs the helper once, counts every requested app (a requested
  app with no result line counts as failed), and writes `.shas` only for `ok`
  apps.
- The session lives in `/depotdownloader-config/steam-manifest-helper/session.json`
  (file 0600, directory 0700) on the existing mount; no new volume.
- The Dockerfile gains a .NET SDK stage, pinned by digest, that runs the helper's
  tests (a failing test, or a run that finds none, fails the build) and publishes
  self-contained for the target architecture. DepotDownloader's download stage is
  removed. CI's amd64 image limit moves from 250 to 275 MiB (Karl, 2026-10-07).

## 3. Alternatives considered

- **Contain, then fix.** Stop the run on the first refusal and rotate the app
  order so a later run starts where the last stopped. Cheaper, but it leaves 1,211
  logins in the design and only caps the damage; the SteamPrefill collision would
  remain on every run that gets far enough. Rejected by Karl in favour of
  removing the cause.
- **Adapt DepotDownloader's code.** It already does exactly this work. But it is
  GPL-2.0, so a derived helper could not be MIT, and the maintained surface is the
  whole downloader. Fresh code on SteamKit2 (LGPL-2.1, used as a separate
  replaceable assembly) is smaller and keeps the licence of our own code clean.
  The depot-selection rules are reproduced from DepotDownloader 3.4.0's behaviour
  and pinned by tests rather than copied.
- **Keep both tools behind a switch.** Would give an instant in-place rollback.
  CI fails any amd64 image over 250 MiB, and DepotDownloader is ~75 MiB of it, so
  both cannot ship; raising the limit far enough for both was not worth carrying
  a tool that is the root cause. Rollback is by image tag instead.

## 4. Consequences

- **One login per run**, replacing 1,211. The Monday job no longer holds the
  account in a rate limit, so the SteamPrefill 06:00 MDT run is not refused.
- **A new .NET component in the image.** The image grows by roughly 11 MiB net
  (about 254 MiB against the 275 MiB limit): +86 MiB for the self-contained
  helper, -75 MiB for DepotDownloader. A second toolchain (the .NET SDK, NuGet
  with exact pins and committed lock files) is now part of the build, and the
  LGPL notice obligations for SteamKit2 apply (see the security audit; the
  notices work must be finished before the first release tag).
- **The `login` command is the only way to renew the session.** With
  DepotDownloader gone, nothing else can mint a token. An imported token is used
  until Steam rejects it; then the run fails loudly with exit 2 or 3, naming the
  `login` command, and the operator runs it once with a TTY and approves on the
  Steam app. A rejected token is never silent.
- **Failures are no longer silent.** Apps the helper never reports are now
  counted as failed (`manifest_fetch.app_unreported`). A run that reports none
  fails the job; partial skips turn it red past
  `fetch_manifests_max_failure_ratio` (0.75). The helper's stderr tail and summary
  are logged on every failure.
- **Rollback is by image tag**, not a switch: `docker tag
  orchestrator:dpa-pre-361 orchestrator:dpa`, then recreate the agent in an
  inter-sweep gap, after syncing `orchestrator-manifests` as
  `docs/deploy/INSTALL.md` §6 requires. No rebuild. Rolling back also restores
  DepotDownloader's 1,211-login behaviour, so it is a fallback, not a steady
  state.
- **Closure evidence** is expected to be the Monday run after deploy:
  `fetch_manifests.done` with `failed` near 0, one Steam login in the helper's
  log, the 06:00 MDT SteamPrefill run ending `END steam prefill ok`, and Kuma 176
  staying UP. #361 closes only on that evidence.

## References

- `docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md`
- `docs/superpowers/plans/2026-10-07-steam-manifest-helper.md`
- `docs/security-audits/steam-manifest-helper-security-audit.md`
- [ADR-0013] — Steam subprocess isolation (superseded; historical context for
  running Steam code in a child process)
- [ADR-0014] — Epic pure-Python manifest (the other manifest source)

[ADR-0013]: 0013-steam-subprocess-isolation.md
[ADR-0014]: 0014-epic-pure-python-manifest.md
