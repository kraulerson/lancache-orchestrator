# UAT Session 18 — Exploratory (Malicious User)

**Scope:** Feature 30, the one-login Steam manifest helper (#361): the C# helper in
`tools/steam_manifest_helper/` (`Cli`, `SessionStore`, `FetchRunner`, `DepotSelector`,
`SteamKitGateway`, `ConnectRetry`, `LoginCommand`), the Python caller
`src/orchestrator/platform/steam/manifest_fetcher.py`, its settings, agent wiring and the
Dockerfile helper stage. Also the 7% alarm: `summarise_tally` in
`src/orchestrator/jobs/handlers/fetch_manifests.py` and
`fetch_manifests_max_failure_ratio`.

**Method:**

- **Code.** I read the code against spec rulings 1-26.
- **Helper.** I ran a scratch Release build of the helper at `c7a3ee2`. Every local run
  went through `sandbox-exec` with all network denied (`(deny network*)`). The block is
  proven: `curl: (6) Could not resolve host: api.steampowered.com` and
  `nc: connectx to 1.1.1.1 port 443 (tcp) failed: Operation not permitted`, while the
  same `nc` succeeds outside the sandbox. In the outputs below:
  - `h.sh` runs that build and prints the exit code, stdout and stderr separately.
  - `pr.sh` runs a scratch console harness compiled against the same source. The harness
    assembly is named `SteamManifestHelper.Tests`, so the helper's `InternalsVisibleTo`
    applies. It calls `Cli.ReadAppIds`, `Cli.ParseOptions`, `SessionStore.*`, and
    `Cli.RunAsync` with a fake `ISteamGateway`; every app gets depot 1001 with gid 42.
- **Python.** I ran the real `SteamManifestFetcher` against stand-in helpers with the repo
  `.venv`. Every repro shown below was re-run on a `git archive c7a3ee2 src` export.
- **Why the export.** The shared checkout moved to `fix/369-depot-key-retry`, with
  uncommitted #369 work, during this session. The earlier exploratory runs used that
  tree's `src`. The files they exercise are byte-identical to `c7a3ee2`
  (`git diff --quiet c7a3ee2 -- <file>`): the fetcher, the handler, the worker, the agent
  client, the agent's steam router, the locator and both manifest parsers. `settings.py`
  is not, which is why the shown repros use the export.
- **Live, on `orchestrator-agent`.** I ran read-only checks, eight helper runs that stop
  before any logon, and **one** Steam logon with a fake account and a fake token. The
  full list and its cleanup are in the "Live probes and cleanup" section.
- **Untouched.** No tracked file was modified. Nothing under `/depotdownloader-config` or
  `/manifest-archive` was written. The real `session.json` and `account.config` were
  never read, only `stat`-ed.

**Verdict:** **No SEV-1 or SEV-2 findings.** The headline promise held under attack:

- **The logon.** The live refusal path exits 2 with `logons:1` in about a second, with no
  retry loop.
- **The exit codes** follow the 0/1/2/3/4/64 contract.
- **No token or CDN URL** reached any output or log I could find. That covers every local
  and live run and 110,852 agent log lines.

**5 SEV-3 findings.** Three are ways the job loses a run, or reads UP, on input it has
not yet seen in production:

- F1: one bad byte in the helper's output discards the whole run.
- F2: an unreadable selection file reads as "nothing to do".
- F3: a partial manifest-format drift reads UP.

The other two:

- F4: the failure text an operator sees cuts off the cause and the `login` command.
- F5: a pre-existing (#213) durability gap. A `.shas` killed mid-write becomes a permanent
  empty sidecar, which hides that depot from validation.

**6 SEV-4 findings:** robustness and polish.

Known issues seen and **not** re-filed: #369, the depot-key `TaskCanceledException`. It
accounts for 148 of the 164 failures on 2026-10-13 and is why Kuma 182 is DOWN; it is
B1/F1 in this session's other results.

## Findings table

| # | Component | Finding | Severity |
|---|---|---|---|
| 1 | `manifest_fetcher._run_helper` / `_parse_stdout` | One undecodable byte (stdout or stderr), one >4,300-digit number or one deeply nested line aborts `fetch_all` and discards every manifest the helper fetched — the docstring and ruling 17 say such a line is skipped | SEV-3 |
| 2 | `manifest_fetcher._enumerate_app_ids` + alarm | A missing, truncated or non-list `selectedAppsToPrefill.json` enumerates **zero** apps, also drops the new-purchase `.bin` delta, and reads UP (`apps=0`) — indistinguishable from a legitimately empty selection | SEV-3 |
| 3 | `fetch_all` drift guard + `summarise_tally` | A partial manifest-format drift reads UP: 9 of 10 apps archiving nothing gives `failed=0` because unparseable manifests count as `skipped`; the guard only fires when *every* manifest is empty | SEV-3 |
| 4 | agent job error → `jobs.error` → Kuma 182 | The operator-facing text is cut before the session problem for `session.json` exit-3 reasons, and before the `login` command for exit 2 | SEV-3 |
| 5 | `manifest_fetcher._write_shas` (pre-existing, #213) | A kill between create and write leaves a 0-byte `.shas` that is never rewritten and, being newest, shadows the depot's `.bin`: that depot is never validated again | SEV-3 |
| 6 | `_enumerate_app_ids` / `_app_ids_with_ext` / `_MANIFEST_NAME_RE` | One malformed selection entry or archive name aborts the run before logon with no `manifest_fetch.*` log; junk entries become real app ids; Unicode digits pass the name regex | SEV-4 |
| 7 | `summarise_tally` | Raises `OverflowError`/`AttributeError` on tallies its contract says read healthy; negative tallies read UP; the message mixes manifest counts with app counts | SEV-4 |
| 8 | `manifest_fetch_timeout_sec` | Accepts `inf` (every run then crashes) and unbounded values (a hung helper then blocks every later fetch until the agent restarts) | SEV-4 |
| 9 | Token custody around `SessionStore` | The 0600 guarantee covers only the file `Save` writes: the live DepotDownloader store holding the imported token is 0644, deleting `session.json` silently re-imports it, a killed `Save` leaves hidden token copies, and loose pre-existing modes are never tightened | SEV-4 |
| 10 | `SessionStore.Load` / `ImportFromDepotDownloader` | No file-type or size check: a FIFO hangs, `/dev/zero` costs 4.4 GB RSS then OOM, a 300 MB file loads; the first unreadable `account.config` aborts the import | SEV-4 |
| 11 | Helper CLI | Logs on before checking it has work or a usable `--out`; unknown/repeated flags accepted silently; `login` without a TTY fails opaquely; several misleading messages | SEV-4 |

---

## Finding 1 — One bad byte or line in the helper's output discards the whole run (SEV-3)

**File:** `src/orchestrator/platform/steam/manifest_fetcher.py`. The relevant code is
`_run_helper` at lines 211-223 and `_parse_stdout` at lines 225-251.

`_parse_stdout`'s docstring says: *"A non-JSON line, a partial trailing line included, is
skipped."* Ruling 17 relies on that. But only `json.JSONDecodeError` is caught (line 236).
Three other shapes escape:

1. **Undecodable bytes.** `subprocess.run(..., text=True)` (lines 212-214) decodes stdout
   **and stderr** strictly. One undecodable byte on either stream raises
   `UnicodeDecodeError` out of `subprocess.run`. The timeout path already decodes with
   `errors="replace"` (`_text`, lines 44-48). The normal path does not.
2. **Huge numbers.** A JSON number over 4,300 digits raises `ValueError`, Python's
   int-string limit, which is not a `JSONDecodeError`.
3. **Deep nesting.** Nesting deeper than the recursion limit raises `RecursionError`.

Each one aborts `fetch_all` before the archive pass. The `TemporaryDirectory` holding
every fetched manifest is then deleted, nothing is archived, and the job fails.

**Repro.** One perfect helper run, three times; the only difference is one extra line:

```
$ PYTHONPATH=<git archive c7a3ee2>/src .venv/bin/python - <<'EOF'
import pathlib, struct, sys, tempfile
from orchestrator.platform.steam.manifest_fetcher import SteamManifestFetcher
ld = lambda f, p: bytes([(f << 3) | 2, len(p)]) + p
payload = ld(1, ld(6, ld(1, bytes.fromhex("ab" * 20))))  # one chunk SHA
root = pathlib.Path(tempfile.mkdtemp())
(root / "good.manifest").write_bytes(struct.pack("<II", 0x71F617D0, len(payload)) + payload)
(root / "dd/steam-manifest-helper").mkdir(parents=True)
(root / "dd/steam-manifest-helper/session.json").write_text("{}")
(root / "cfg").mkdir()
(root / "cfg/selectedAppsToPrefill.json").write_text("[10]")
for name, extra in [("control", ":"),
                    ("stderr", r"printf 'connect attempt failed: \377\n' >&2"),
                    ("stdout", "printf '{\"app\": %s}\\n' \"$(printf '9%.0s' $(seq 5000))\"")]:
    helper = root / f"helper-{name}.sh"
    helper.write_text(
        "#!/bin/sh\n"
        'OUT="$5"; mkdir -p "$OUT/10"\n'
        f'cp "{root}/good.manifest" "$OUT/10/11_100.manifest"\n'
        "printf '%s\\n' '{\"app\":10,\"status\":\"ok\",\"manifests\":[\"11_100.manifest\"]}'\n"
        f"{extra}\n"
        "printf '%s\\n' '{\"summary\":true,\"session\":\"completed\",\"reason\":\"\",\"logons\":1}'\n")
    helper.chmod(0o755)
    archive = root / f"archive-{name}"
    f = SteamManifestFetcher(binary=helper, config_dir=root / "dd", steam_config_dir=root / "cfg",
                             archive_dir=archive, username="u")
    try:
        sys.stdout.write(f"[{name}] {f.fetch_all()}\n")
    except Exception as e:
        sys.stdout.write(f"[{name}] RAISES {type(e).__name__}: {str(e)[:90]}\n")
    sys.stdout.write(f"[{name}] .shas archived: {sorted(p.name for p in archive.glob('v1/*.shas'))}\n")
EOF
[info     ] manifest_fetch.done            apps=1 failed=0 fetched=1 helper_exit=0 helper_outcome=completed logons=1 ...
[control] FetchResult(fetched=1, skipped=0, failed=0, apps=1)
[control] .shas archived: ['10_10_11_100.shas']
[error    ] manifest_fetch.run_aborted     reason="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 24: invalid start byte"
[stderr] RAISES UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 24: invalid start byte
[stderr] .shas archived: []
[error    ] manifest_fetch.run_aborted     reason='ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 5000 digits; ...'
[stdout] RAISES ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 5000 digits; use
[stdout] .shas archived: []
```

The same probe kit gave the same abort for a non-UTF-8 line on **stdout**
(`UnicodeDecodeError ... byte 0xc3 ... invalid continuation byte`). A line nested
100,000 deep gave
`RecursionError: maximum recursion depth exceeded while decoding a JSON array`.

**Reachability:** not from today's helper.

- Its stdout is System.Text.Json output, which escapes every non-ASCII character.
- Its stderr is UTF-8 text the helper builds itself.

This is a latent contract gap, but the blast radius is large: one byte, from any future
log line or runtime warning, discards about an hour of Steam traffic and that week's
coverage.

**Fix shape:**

- Decode with `errors="replace"`, as `_text` already does.
- Catch `ValueError` and `RecursionError` per line. `JSONDecodeError` is a `ValueError`.

---

## Finding 2 — An unreadable selection file reads as "nothing to do", and the alarm stays UP (SEV-3)

**File:** `manifest_fetcher.py`, `_enumerate_app_ids`, lines 123-155. Also
`fetch_manifests.py`, lines 57-58.

`_enumerate_app_ids` returns `[]` when `selectedAppsToPrefill.json` is missing, is not
JSON, or is not a list. That early `return []` also skips the `.bin`-without-`.shas`
delta at lines 151-155, which is the net for recently purchased games. The rest follows:

- `fetch_all` logs `manifest_fetch.no_apps` and returns `FetchResult(0, 0, 0, 0)`.
- `summarise_tally` reports that UP, because `apps <= 0`.

**How this differs from ruling 21(a).** The ruling accepted UP for an *empty selection*,
and a valid `[]` still sends the delta app to the helper. The unreadable cases differ:

- The run silently does nothing, for new purchases too.
- The Kuma 182 message is a bare `apps=0 fetched=0 skipped=0 failed=0`.
- The warning is the same event a legitimately empty selection logs.

**Repro:**

```
$ PYTHONPATH=<git archive c7a3ee2>/src .venv/bin/python - <<'EOF'
import dataclasses, pathlib, sys, tempfile
from orchestrator.platform.steam.manifest_fetcher import SteamManifestFetcher
from orchestrator.jobs.handlers.fetch_manifests import summarise_tally
for label, content in [("valid, empty list []", "[]"), ("truncated mid-write", "[730, 440, 57"),
                       ("missing", None), ("JSON object, not a list", '{"apps": [730]}')]:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "dd/steam-manifest-helper").mkdir(parents=True)
    (root / "dd/steam-manifest-helper/session.json").write_text("{}")
    (root / "cfg").mkdir()
    if content is not None:
        (root / "cfg/selectedAppsToPrefill.json").write_text(content)
    (root / "archive/v1").mkdir(parents=True)  # a recently purchased game: .bin, no .shas yet
    (root / "archive/v1/999_999_1000_5.bin").write_bytes(b"x")
    f = SteamManifestFetcher(binary=pathlib.Path("/usr/bin/false"), config_dir=root / "dd",
                             steam_config_dir=root / "cfg", archive_dir=root / "archive", username="u")
    apps = f._enumerate_app_ids()
    res = f.fetch_all() if not apps else None
    tally = summarise_tally(dataclasses.asdict(res), 0.07) if res else "(helper would run)"
    sys.stdout.write(f"{label:26} enumerates {apps!s:7} -> {tally}\n")
EOF
valid, empty list []       enumerates [999]   -> (helper would run)
[warning  ] manifest_fetch.no_apps
truncated mid-write        enumerates []      -> (True, 'apps=0 fetched=0 skipped=0 failed=0')
[warning  ] manifest_fetch.no_apps
missing                    enumerates []      -> (True, 'apps=0 fetched=0 skipped=0 failed=0')
[warning  ] manifest_fetch.no_apps
JSON object, not a list    enumerates []      -> (True, 'apps=0 fetched=0 skipped=0 failed=0')
```

**How a bad file gets there** (from the code; not reproduced live). The file is rewritten
with plain truncate-then-write `write_text` in two places:

- `prefill_driver.py:199/204`: `prefill_apps` writes a temporary list, then restores the
  operator's list from memory in a `finally`.
- `agent/routers/steam.py`: prune-selection.

A kill between the truncate and the write leaves an empty or partial file. The same code
shows a worse sibling, outside this feature's scope: a kill during a force-prefill leaves
the *temporary* list in place as valid JSON, which the fetcher cannot detect at all.
SteamPrefill's own cron reads the same file, so Kuma 176 may also notice a corrupt file;
I did not verify that.

**Fix shape:** treat "unreadable" differently from "empty". Raise, or return a result
that reads DOWN, and still compute the `.bin` delta.

---

## Finding 3 — A partial manifest-format drift reads UP (SEV-3)

**File:** `manifest_fetcher.py`, lines 391-399 (the ruling-26 drift guard).
`fetch_manifests.py`, line 60.

The drift guard fires only when every listed manifest parses empty. Any mix with at least
one parseable manifest counts the others as `skipped`, and `summarise_tally` looks only
at `failed`.

**Repro.** Ten apps report `ok`, and 9 of their manifests have the right magic but an
unreadable body:

```
$ PYTHONPATH=<git archive c7a3ee2>/src .venv/bin/python - <<'EOF'
import dataclasses, json, pathlib, struct, sys, tempfile
from orchestrator.platform.steam.manifest_fetcher import SteamManifestFetcher
from orchestrator.jobs.handlers.fetch_manifests import summarise_tally
ld = lambda f, p: bytes([(f << 3) | 2, len(p)]) + p
good = ld(1, ld(6, ld(1, bytes.fromhex("ab" * 20))))
good = struct.pack("<II", 0x71F617D0, len(good)) + good
drifted = struct.pack("<II", 0x71F617D0, 64) + b"\x00" * 64  # right magic, unreadable body
root = pathlib.Path(tempfile.mkdtemp())
apps = list(range(10, 110, 10))
lines = []
for i, app in enumerate(apps):
    (root / f"prepared/{app}").mkdir(parents=True)
    (root / f"prepared/{app}/{app + 1}_7.manifest").write_bytes(good if i == 0 else drifted)
    lines.append(json.dumps({"app": app, "status": "ok", "manifests": [f"{app + 1}_7.manifest"]}))
lines.append(json.dumps({"summary": True, "session": "completed", "reason": "", "logons": 1}))
(root / "stdout.txt").write_text("\n".join(lines) + "\n")
helper = root / "helper.sh"
helper.write_text(f'#!/bin/sh\nOUT="$5"; mkdir -p "$OUT"; cp -R "{root}/prepared/." "$OUT/"\n'
                  f'cat "{root}/stdout.txt"\n')
helper.chmod(0o755)
(root / "dd/steam-manifest-helper").mkdir(parents=True)
(root / "dd/steam-manifest-helper/session.json").write_text("{}")
(root / "cfg").mkdir()
(root / "cfg/selectedAppsToPrefill.json").write_text(json.dumps(apps))
f = SteamManifestFetcher(binary=helper, config_dir=root / "dd", steam_config_dir=root / "cfg",
                         archive_dir=root / "archive", username="u")
res = f.fetch_all()
sys.stdout.write(f"{res}\nalarm: {summarise_tally(dataclasses.asdict(res), 0.07)}\n")
sys.stdout.write(f".shas archived: {len(list((root / 'archive/v1').glob('*.shas')))} of 10 apps\n")
EOF
[warning  ] manifest_fetch.empty_manifest  app_id=20 name=21_7.manifest
  ... (same warning for apps 30, 40, 50, 60, 70, 80, 90, 100)
[info     ] manifest_fetch.done            apps=10 failed=0 fetched=1 helper_exit=0 helper_outcome=completed logons=1 not_attempted=0 reason= skipped=9 stderr_tail=
FetchResult(fetched=1, skipped=9, failed=0, apps=10)
alarm: (True, 'apps=10 fetched=1 skipped=9 failed=0')
.shas archived: 1 of 10 apps
```

**Live baseline, for calibrating a better guard.** Counted with `grep -c` in the agent
log:

| Run | `manifest_fetch.empty_manifest` | Manifests parsed | Share empty |
|---|---|---|---|
| 2026-10-08 | 35 | 3,257 | ≈1.1% |
| 2026-10-13 | 29 | 2,630 | ≈1.1% |

A guard on the empty **share**, for example above 10%, would not trip on the legitimate
zero-chunk depots. Today's guard tolerates every share up to (n-1)/n.

**Realistic trigger:** a SteamKit2 upgrade that changes `SaveToFile`'s layout for some
manifests, for example only newly built ones. The re-fetched manifests of changed games,
which are the reason this job exists, would then stop producing `.shas` while Kuma 182
stays UP.

---

## Finding 4 — The failure text an operator sees loses the cause and the `login` command (SEV-3)

**What the spec requires.** Spec §2: *"Token import fails | Exit 3. The job fails, and
the reason names the `login` command."* The helper's reason does name it, but the text is
cut twice before an operator sees it:

1. The agent job error is cut to 200 characters (`agent/routers/steam.py:249`).
2. The worker records `f"{type(e).__name__}: {str(e)[:150]}"` (`jobs/worker.py:250`).
   That string becomes `jobs.error` and the Kuma 182 DOWN message (`worker.py:85`).

The `session.json` exit-3 reasons start with the 58-character path, so the cut lands
before the problem itself.

**Repro:**

```
$ PYTHONPATH=<git archive c7a3ee2>/src .venv/bin/python - <<'EOF' 2>&1 | grep -v manifest_fetch.helper_failed
import json, pathlib, sys, tempfile
from orchestrator.platform.steam.manifest_fetcher import SteamManifestFetcher
from orchestrator.clients.agent_client import AgentError
from orchestrator.jobs.worker import JOB_ERROR_TRUNCATE
SESS = "/depotdownloader-config/steam-manifest-helper"
for code, reason in [
    (3, f"{SESS}/session.json holds a login for a different account."
        f" Run: SteamManifestHelper login --username kraulerson --session-dir {SESS}"),
    (2, "Steam rejected the saved login (Expired). Run: SteamManifestHelper login"
        " --username kraulerson --session-dir <session dir>"),
]:
    root = pathlib.Path(tempfile.mkdtemp())
    summary = {"session": "x", "reason": reason, "logons": 1, "summary": True}
    helper = root / "helper.sh"
    helper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{json.dumps(summary)}'\nexit {code}\n")
    helper.chmod(0o755)
    (root / "dd/steam-manifest-helper").mkdir(parents=True)
    (root / "dd/steam-manifest-helper/session.json").write_text("{}")
    (root / "cfg").mkdir()
    (root / "cfg/selectedAppsToPrefill.json").write_text("[10]")
    f = SteamManifestFetcher(binary=helper, config_dir=root / "dd", steam_config_dir=root / "cfg",
                             archive_dir=root / "archive", username="kraulerson")
    try:
        f.fetch_all()
    except Exception as e:
        agent_error = f"{type(e).__name__}: {e}"[:200]          # agent/routers/steam.py:249
        ae = AgentError(f"agent job failed: {agent_error}")     # clients/agent_client.py:211
        shown = f"{type(ae).__name__}: {str(ae)[: JOB_ERROR_TRUNCATE - 50]}"  # jobs/worker.py:250
        sys.stdout.write(f"exit {code} -> jobs.error and Kuma 182 msg:\n  {shown}\n")
EOF
[info     ] manifest_fetch.done   ... helper_exit=3 ... reason='/depotdownloader-config/steam-manifest-helper/session.json holds a login for a different account. Run: SteamManifestHelper login --username kraulerson --session-dir /depotdownloader-config/steam-manifest-helper' ...
exit 3 -> jobs.error and Kuma 182 msg:
  AgentError: agent job failed: HelperSessionError: steam manifest helper stopped (session import failed): /depotdownloader-config/steam-manifest-helper/session.jso
[info     ] manifest_fetch.done   ... helper_exit=2 ... reason='Steam rejected the saved login (Expired). Run: SteamManifestHelper login --username kraulerson --session-dir <session dir>' ...
exit 2 -> jobs.error and Kuma 182 msg:
  AgentError: agent job failed: HelperSessionError: steam manifest helper stopped (login failed): Steam rejected the saved login (Expired). Run: SteamManifestHelper
```

**Workaround.** The whole reason survives in the agent's own `manifest_fetch.done` and
`manifest_fetch.helper_failed` lines on the NAS, so a workaround exists.

**How this meets INSTALL.md §6.** The doc tells the operator to look for two texts:

- "holds a login for a different account" never reaches `jobs.error` or Kuma.
- "Steam rejected the saved login" does, but the command is cut at
  `Run: SteamManifestHelper`.

**Related, cosmetic.** The exit-2 advice uses a literal `--session-dir <session dir>`
(`FetchRunner.cs:38`). I confirmed it on the live one logon:
`"reason":"Steam rejected the saved login (InvalidPassword). Run: SteamManifestHelper login --username uat18probe_9f3c2a7d1e5b --session-dir <session dir>"`.

**Why it matters soon.** The imported token will expire (spec Risks, "Token lifetime"),
and nothing reads its expiry ahead of time. The first notice will be this truncated DOWN
message.

**Fix shape:** put the cause first and the path last, as in
`holds a login for a different account (<path>)`, or shorten the `HelperSessionError`
prefix.

---

## Finding 5 — A `.shas` write killed mid-flight becomes a permanent empty sidecar that hides its depot (SEV-3, pre-existing since #213)

**Files:**

- `manifest_fetcher.py:173-186`, `_write_shas`. It is unchanged by #361.
- `agent/routers/steam.py:403`.
- `agent/manifest_locator.py:94`.

**The defect.** `_write_shas` checks `out.exists()` and then calls `out.write_text(...)`,
which is not atomic. A kill after the create and before the write leaves an empty file.
Every later run sees that it exists and skips it ("append-only archive").

**The consequence.**

- The validator reads it with `parse_shas`, which returns 0 SHAs.
- The locator picks the newest file per depot by mtime, so the empty `.shas` also
  shadows SteamPrefill's `.bin` for the same depot and gid.
- That depot's chunks are never checked again until its gid changes.
- With the game's other depots cached, the game can read as cached while that depot is
  evicted.

**Repro (kill).** A child process writes one 1,000,000-chunk `.shas`, and the parent
sends SIGKILL as soon as the file exists but is short:

```
$ .venv/bin/python - <scratch>/shaskill <<'EOF'   # child: SteamManifestFetcher(...)._write_shas(10, 11, "777", 1M shas)
...
after SIGKILL mid-write: 10_10_11_777.shas exists=True size=0 (complete = 41000000)
next run: _write_shas for the same app/depot/gid returned False; size still 0
validator parse_shas reads 0 of 1000000 chunk SHAs from it
```

**Repro (shadowing).** I placed SteamPrefill's `.bin` for the same depot and gid, one day
older:

```
>>> locate_manifest_bins(10, cache_roots=[root], prefilled_gids={"777"})
locator picks for depot 11: ['10_10_11_777.shas (0 bytes)']
```

**Reachability:** low. It needs a SIGKILL, from a container recreate or an OOM kill,
during the few-second archive burst at the end of a weekly run. It is flagged anyway,
because it is a silent false-green path for cache truth. An earlier attempt sent 6
SIGKILLs at random times and left no damaged file among the 17 written. The window is
real but narrow.

**Fix shape:**

- Write to a temporary name, then `os.replace` it into place.
- Treat a 0-byte `.shas` as absent.

---

## Finding 6 — One malformed enumeration input aborts the run before logon, unlogged (SEV-4)

**File:** `manifest_fetcher.py`. The code sits at lines 128-137, 169-170 and 38.

**Three ways one bad input aborts the run:**

- **The selection's `int(k)` coercion** catches `(TypeError, ValueError)` but not
  `OverflowError`. `1e400` and `Infinity`, both accepted by `json.loads`, raise it.
- **`json.loads` itself** raises a plain `ValueError` for a 5,000-digit integer. That is
  not a `JSONDecodeError`, so line 128 does not catch it.
- **`_app_ids_with_ext`** uses `str.isdigit()`, which accepts a superscript `²` that
  `int()` rejects.

Each one aborts the whole run, the same class ruling 22 (M10) fixed for out-of-range ids.
`_enumerate_app_ids()` runs *before* the `try` (line 311 versus 317), so no
`manifest_fetch.run_aborted` is logged either.

**Two quieter quirks:**

- Junk entries are coerced into real app ids.
- `_MANIFEST_NAME_RE`'s `\d` accepts Unicode digits, and the un-normalised gid goes into
  the archive file name.

```
$ PYTHONPATH=<git archive c7a3ee2>/src .venv/bin/python -   # f._enumerate_app_ids() per selection; _MANIFEST_NAME_RE.fullmatch
[730, 1e400]           -> RAISES OverflowError: cannot convert float infinity to integer
[730, Infinity]        -> RAISES OverflowError: cannot convert float infinity to integer
5000-digit id          -> RAISES ValueError: Exceeds the limit (4300 digits) for integer string conversio
superscript .bin name  -> RAISES ValueError: invalid literal for int() with base 10: '²'
junk entries           -> apps [1, 3, 440, 570, 730]          # from [730, true, 440.9, "57_0", "٣"]
fullwidth name '１１_１００.manifest' matches: True; .shas name would be 10_10_11_１００.shas
```

**Reachability:** none from SteamPrefill's own writes (plain integers) or from the helper
(ASCII digits). Only a hand edit or corruption reaches it.

---

## Finding 7 — `summarise_tally` raises on tallies its contract says read healthy; the message mixes units (SEV-4)

**File:** `fetch_manifests.py`, lines 39-60.

**The contract.** The function promises that a malformed tally reads healthy. Its comment
warns that a raise "would mark the JOB failed and push the heartbeat DOWN, manufacturing
exactly the false alarm" it forswears. But `_count` catches only `TypeError` and
`ValueError`:

```
F7 apps=0, failed=50        -> (True, 'apps=0 fetched=0 skipped=0 failed=50')
F7 apps=100, failed=-40     -> (True, 'apps=100 fetched=0 skipped=0 failed=-40')
F7 failed: Infinity (JSON)  -> RAISES OverflowError: cannot convert float infinity to integer
F7 failed: 1e400 (JSON)     -> RAISES OverflowError: cannot convert float infinity to integer
F7 apps=1, failed=10**400   -> RAISES OverflowError: integer division result too large for a float
F7 tally is a list          -> RAISES AttributeError: 'list' object has no attribute 'get'
```

This is unreachable from the real agent, whose `FetchResult` is four ints.

**Mixed units.** `fetched` and `skipped` count **manifests** (lines 344-346 sum
`_archive_app`'s per-manifest counts). `apps` and `failed` count **apps**. The live
10-13 Kuma 182 message was `apps=1212 fetched=52 skipped=2578 failed=164`:

- `skipped` is larger than `apps`.
- The number of apps that succeeded appears nowhere.

This is pre-existing (#294), but it is the text on Kuma 182.

---

## Finding 8 — `manifest_fetch_timeout_sec` accepts `inf` and unbounded values (SEV-4)

**File:** `src/orchestrator/core/settings.py:150` at `c7a3ee2`, which reads
`Field(default=7200.0, gt=0.0)`.

```
F8 ORCH_MANIFEST_FETCH_TIMEOUT_SEC='inf'  -> accepted as inf
F8 ORCH_MANIFEST_FETCH_TIMEOUT_SEC='1e9'  -> accepted as 1000000000.0
F8 ORCH_MANIFEST_FETCH_TIMEOUT_SEC='nan'  -> rejected (ValidationError)
[error    ] manifest_fetch.run_aborted     reason='OverflowError: cannot convert float infinity to integer'
F8 fetch_all with timeout_sec=inf -> RAISES OverflowError: cannot convert float infinity to integer
```

**Two failure modes:**

- **`inf`.** Every fetch fails immediately, because `subprocess.run(timeout=inf)`
  overflows.
- **A huge value.** The 2-hour hang protection is gone:
  1. The control plane gives up at `job_max_runtime_sec` (21600 s).
  2. The agent's single-flight keeps returning the hung job's id to every later POST.
  3. No fetch can run until the agent restarts.

By comparison, the ratio setting rejects `nan`, `inf` and `"7%"`.

**Fix shape:** add an upper bound (`le=`) and `allow_inf_nan=False`.

---

## Finding 9 — The 0600 guarantee covers only the file `Save` writes (SEV-4)

Ruling 1 makes `session.json` private from creation, and it is: `600 root:root` in a
`700` directory, live. But copies of the same credential sit outside that guarantee:

**(a) The live DepotDownloader store is world-readable.** The store the helper imported
from is world-readable (read-only `stat` inside the container):

```
700 root:root /depotdownloader-config/steam-manifest-helper
600 root:root /depotdownloader-config/steam-manifest-helper/session.json
644 orchestrator:orchestrator /depotdownloader-config/.local/share/IsolatedStorage/h0aysnz4.3by/n5abilyo.0u5/Url.usb1eufajzvjvmsobi3h2uqb0wew010h/AssemFiles/account.config
2026-10-08 14:21:34.996353404 +0000 /depotdownloader-config/steam-manifest-helper/session.json      (the import)
2026-07-01 03:00:00.000000000 +0000 .../AssemFiles/account.config                                  (never touched since)
```

**Does it still hold the same token?** Unless `login` has run since 10-08, the store
still holds the token that is now in `session.json`. I could not confirm identity without
reading either file, which is out of bounds.

**Exposure today is bounded:**

- `orchestrator-agent` is the only running container that mounts the volume, and it runs
  as `0:0`.
- On the host, `/volume2/docker-storage` is `710 root:root`. As `karl`, `stat` fails with
  `Permission denied`, and `[ -r ]` is false for both token files.

So this is defence in depth, not a live leak.

**(b) Deleting `session.json` is not a logout.** Python always passes `--import-from`
(`manifest_fetcher.py:208-209`), so the next run silently re-imports the same token. Run
through `pr.sh cli-fake ... --import-from <dd>`:

```
### round 1: session.json present? no
exit=0 logonCalls=1 saved=1
-rw------- reimp/dd/steam-manifest-helper/session.json
-rw-r--r-- reimp/dd/.local/share/IsolatedStorage/a/b/c/AssemFiles/account.config
session.json holds the DD store's token: 1
(operator deletes session.json to log the helper out)
### round 2: session.json present? no
exit=0 logonCalls=1 saved=1
-rw------- reimp/dd/steam-manifest-helper/session.json
session.json holds the DD store's token: 1
```

**(c) A killed `Save` leaves hidden token copies.** They are never cleaned up. I sent
SIGKILL to a `Save` loop 10 times; 8 kills left a full copy (mode 0600):

```
-rw-------@ 71 .session.json.1evh03az.odx   ... (8 files) ...
killsave/.session.json.1evh03az.odx sentinel=1   ... (all 8: sentinel=1)
```

The live directory has none today: `ls -lA` shows only `-rw------- root session.json`.

**(d) Loose pre-existing modes are accepted and never tightened.** A `0644`
`session.json` loads without complaint, and a `0777` directory is never tightened:

```
LOADED user=uat18-fake tokenLen=24 (19 ms) ...
drwxrwxrwx loose
-rw-r--r-- loose/session.json
```

**Fix shape:**

- After a successful import, `chmod 0600` the DepotDownloader store or delete it.
- On load, warn about any group- or world-readable session file.

---

## Finding 10 — The session and store files are read with no type or size check (SEV-4)

**File:** `SessionStore.cs`. `Load` is at lines 40-45 (`File.Exists` then
`File.ReadAllText`). The import loop is at lines 81-99.

```
### session.json is a FIFO (no writer) - 20 s watchdog
HUNG: still blocked after 20 s (killed by watchdog)
### session.json -> /dev/zero, real CLI (h.sh fetch ...)
exit=1
unexpected: OutOfMemoryException: the helper stopped on an error it does not handle (message withheld)
        1.40 real         0.94 user         0.47 sys
  4411375616  maximum resident set size
### 300 MB valid session.json (giant extra field)
LOADED user=uat18-fake tokenLen=22 (301 ms) ...
  1330757632  maximum resident set size
### import: first account.config (a/) is not deflate, second (b/) is valid
THROWS SessionImportException: cannot read .../imp/two/a/account.config: InvalidDataException
### import: two valid stores, different tokens for the same user
IMPORTED user=uat18-fake tokenLen=23 token-is-sentinel=True   (23 = the first store's token; the second's is 27)
```

**Production consequences:**

- **FIFO.** The fetch would hang until Python kills it at 7,200 s.
- **`/dev/zero`.** It costs 4.4 GB inside an 8 GiB agent that has been OOM-killed before.

Only root on the config volume can plant these files.

---

## Finding 11 — Helper CLI rough edges (SEV-4)

- **It logs on before it knows it has work or somewhere to write.** Run through
  `pr.sh cli-fake` (`FetchRunner.cs:25` runs before the loop; `:136-137`):
  - An empty `--apps` file costs a logon:
    `exit=0 logonCalls=1` / `{"session":"completed","reason":"","logons":1,"summary":true}`
    / `logged on as uat18-fake; fetching 0 apps`.
  - An unusable `--out` fails after logon with exit 1 and no app lines:
    - `--out` a file: `exit=1 logonCalls=1` /
      `unexpected: DirectoryNotFoundException ...`.
    - `--out /`: `exit=1 logonCalls=1` / `unexpected: IOException ...`.

  Python guards both (the M5a skip, and a fresh temporary directory), so the agent cannot
  reach either one.
- **Unknown and repeated flags are accepted silently, and the last one wins.**
  `pr.sh parseopts --apps a --apps b --username x --username y --zzz q` gives
  `--apps=b | --username=y | --zzz=q`. A typo like `--import-frm` would be ignored.
- **`login` without a TTY fails opaquely.** Without a TTY, for example `docker exec`
  without `-t`, `echo pw | h.sh login --username uat18-fake --session-dir ...` gives
  `exit=1` and
  `Steam password for uat18-fake: unexpected: InvalidOperationException: the helper stopped on an error it does not handle (message withheld)`.
  The run stops before any connect. INSTALL.md §6 does use `-it`, but the error gives no
  hint.
- **Misleading messages:**
  - A `session.json` with no `Username` is reported as "holds a login for a different
    account".
  - A `session.json` that is a directory is reported as "no session at …".
  - An empty `--username` gives the advice `Run: SteamManifestHelper login --username  --session-dir /tmp/uat18-live/good`.
    I verified this on the live binary.

---

## Held — tried to break it, could not

- **No token in any output.**
  - Every fake `session.json` and DepotDownloader store carried a `UAT18SENTINELTOKEN-*`
    marker. Across every local run, live batch 1 and the live logon, it appeared
    **0** times in stdout or stderr. Live batch 1 reported
    `sentinel occurrences across every stdout+stderr above: 0`, and the logon reported
    `sentinel occurrences in stdout+stderr: 0`.
  - `SteamSession.ToString()` printed `SteamSession { Username = uat18-fake }`.
  - `SteamKitGateway.Describe` drops URL-bearing messages. Fed exceptions whose messages
    held `?token=SECRET`, it returned `HttpRequestException`,
    `HttpRequestException: HTTP 403`, `TaskCanceledException` and
    `InvalidOperationException`.
- **No token in live logs.**
  - The agent's logs since 2026-10-07 hold 110,852 lines.
  - Counts from `grep -c` only: 0 JWT-shaped strings (`ey[JA]` plus 30 base64url
    characters), 0 lines with `token=`, `access_token` or `refresh_token`, 0 CDN
    `/depot/` URLs, and 0 `RefreshToken`.
- **The live refusal path.** It used a fake account and a fake token:
  - It exited 2 in 1 second, with
    `{"session":"login_refused","reason":"Steam rejected the saved login (InvalidPassword). Run: ...","logons":1,"summary":true}`.
  - There was no connect retry or logon loop.
  - Nothing was written to the session directory, and `--out` stayed empty.
  - `InvalidPassword` maps to `TokenRejected`, so the advice to run `login` is printed.
- **The exit-code contract**, observed locally and live:
  - **64**, with 0 bytes on stdout: no arguments, an unknown command, an odd argument
    count, a missing option, and the `fetch -- --apps …` trick.
  - **3**, with exactly one JSON summary and `logons:0`: every `session.json` problem
    (absent, invalid JSON, wrong account, empty token, numeric token, a directory, a
    `/dev/null` symlink).
  - **1**, with an empty stdout: `--apps` problems, which fail before the gateway exists
    (`FormatException`, `OverflowException`, `FileNotFoundException`). This was verified
    live, and `--out` was never created.
  - **2**: the live logon.
  - **0**: through the fake gateway.
- **The apps file** parsed correctly with CRLF line endings, UTF-8 and UTF-16 BOMs, NBSP
  and tab padding, duplicates (de-duplicated), and 2,000,000 ids.
- **Concurrent saves.** 2,000 parallel `SessionStore.Save` calls into a fresh directory
  gave `errors=0`, a single `session.json` with mode `-rw-------` in a `drwx------`
  directory, valid JSON, and no temporary files left over.
- **The importer** held:
  - A deflate bomb (521 KB inflating to 512 MiB, claiming a 2 GB field) was rejected with
    `ProtoException` in 0.07 s at 62 MB RSS.
  - A symlink loop under `--import-from` still imported, in 0.1 s.
  - A username differing only in case imported.
- **Manifest-name traversal.** Seven shapes were rejected, each logged with
  `pattern_ok:false`, and the app was counted as failed: `../../../../tmp/…`,
  `/etc/passwd`, `11_100.manifest/../../x`, a trailing newline, upper case, a leading
  space, and `11_-100.manifest`. A 313-character all-digit name was skipped cleanly.
- **Stdout shapes** all held:
  - A summary before the app lines.
  - Two summaries (the last wins, logged only).
  - Last-wins per app: `ok` then `error` drops the `ok`; `error` then `ok` archives.
  - A 10 MB line.
  - Unrequested ids, which are ignored.
  - `summary:true` on an app line.
  - `logons:true`, logged as `null`.
  - Two objects on one line fail loudly: `manifest fetch failed for all 2 apps`.
- **Timeouts.** Apps reported before a hang were archived; then `helper_timed_out` was
  logged and the run raised at 2.0 s. A helper that finished and then hung on exit is
  still a failure, which is correct under ruling 17.
- **Out-of-range ids.** `-5`, `0` and `2^32` were dropped before the helper, logged as
  `manifest_fetch.app_id_out_of_range` and counted as failed. Only `730` was sent.
- **Alarm boundaries:**
  - 7/100 is UP and 8/100 is DOWN.
  - 84/1211 is UP and 85/1211 is DOWN.
  - 14/200, exactly 0.07, is UP.
  - The ratio setting rejects `nan`, `inf` and `"7%"`.
- **The alarm on live data.** Healthy runs do not trip it:

  | Run | Failed | `error` | `no_depots` | `not_owned` |
  |---|---|---|---|---|
  | 2026-10-08 | 59 | 47 | 11 | 1 |
  | 2026-10-13 | 164 | 152 | 11 | 1 |

  - The permanent floor is about 1% (12 of 1,212).
  - The 10-13 DOWN is #369: 148 of the 152 errors are
    `depot key N: TaskCanceledException`.
- **Session custody, live.** The directory is `700 root:root` and the file
  `600 root:root`. There were no stray temporary files, and the host path is root-only.

---

## Live probes and cleanup

Every command went through `ssh karl@192.168.1.30`.

- **The hold.** The controller's #369 hold covered helper runs that reach Steam. It was
  in force until about 17:10 UTC, and only local, network-denied probes ran during it.
- **The logon.** The one logon ran at 17:12:40, before the 17:45 cutoff.

| UTC | Command | Writes | Reaches Steam |
|---|---|---|---|
| 17:11:02 | `docker exec orchestrator-agent sh -c "id; ls -la <binary>; stat <config dirs, session.json>; ls -lA <session dir> (modes/names); find /depotdownloader-config -name account.config -exec stat …; df -h /tmp; grep ' /tmp ' /proc/mounts; which timeout"` | none | no |
| 17:11 | `docker inspect orchestrator-agent --format '{{range .Mounts}}…'` (mounts only, no env) | none | no |
| 17:11 | host, as `karl`: `stat` of the `/volume2/…` path components; `sudo -n true` (no sudo); `[ -r ]` on `.local`, `account.config`, `session.json` (no reads) | none | no |
| 17:11 | `docker exec … stat -c "%y %n"` of `session.json` and `account.config`; `docker inspect` of every running container for a `depotdownloader-config` mount | none | no |
| 17:11:55 | `docker exec -i orchestrator-agent sh -s < live1.sh`: 8 helper runs that stop at usage, session load or apps parsing (each verified locally first to stop before the gateway is built) | `/tmp/uat18-live/*` | no |
| 17:12:40-41 | `docker exec -i orchestrator-agent sh -s < live-login.sh`: **the one logon**. Fake account `uat18probe_9f3c2a7d1e5b`, fake token in `/tmp/uat18-live/login/sess/session.json`, `--apps` = `10`, `--out /tmp/uat18-live/login/out`, `timeout 300` | `/tmp/uat18-live/login/*` | **yes (1)** |
| 17:13-18:09 | read-only `docker logs orchestrator-agent --since/--until … \| grep` for `manifest_fetch.*` summaries, warning counts and token-pattern counts (`grep -c` only) | none | no |
| 17:13-18:09 | per-run `app_failed` breakdown. **Deviation:** this wrote `/tmp/uat18-af.txt` on the NAS *host* (filtered log lines: app ids, statuses, reasons; no credentials) and removed it in the same command | host `/tmp/uat18-af.txt`, removed | no |
| 18:09 | read-back `find /tmp/uat18-live -printf …` (19 entries: 8 dirs, 11 files, all mine) and a `/proc` scan for a running helper (`no-helper-process-running`); change card printed | none | no |
| 18:09 | `docker exec orchestrator-agent rm -rf /tmp/uat18-live` → `rm exit=0` | removal | no |
| 18:10-18:11 | verify: container `ls -d /tmp/uat18-*` → `no-uat18-paths-left`; the session dir still holds only `-rw------- root session.json`, mtime unchanged `2026-10-08 14:21:34`; container `running started=2026-10-08T14:19:18Z restarts=0`; host `ls /tmp/uat18-*` → `host: no /tmp/uat18-* files` | none | no |

**Steam logins used: 1.** It used a fake account and a fake token, and Steam answered
`InvalidPassword`. Karl's account was never presented to Steam by this session.

## Notes (code-read only, not reproduced)

- **Local write failures.** `SteamKitGateway.SaveManifestAsync` handles an `IOException`
  from `manifest.SaveToFile` (a full disk, for example) like a bad CDN server: it
  re-downloads the same manifest from up to 6 servers before failing the app
  (`SteamKitGateway.cs:283-303`). Testing this needs the real CDN.
- **PICS requests.** Spec §1 step 3 says app info is requested "in batches". The gateway
  makes two PICS round trips per app (`SteamKitGateway.cs:199-205`). This is context for
  #369's request pacing, not a finding.
- **Out of scope here:** `prefill_driver.prefill_apps` restores the operator's selection
  only from memory. See Finding 2.
- **The shared checkout.** During this session it moved from `c7a3ee2` to
  `fix/369-depot-key-retry` at `56bd10f`, with uncommitted #369 changes, from another
  session. The scratch helper build matches `c7a3ee2` file for file (`cmp` on
  `SteamKitGateway.cs`, `Cli.cs`, `FetchRunner.cs` and `SessionStore.cs`).
