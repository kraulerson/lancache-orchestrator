# Security audit — Steam manifest helper

**Date:** 2026-10-07
**Branch:** `design/361-steam-manifest-helper`
**Phase:** 2.4 (Build Loop `steam-manifest-helper`, step 4 of 6)
**Persona:** Senior Security Engineer — hunt vulnerabilities, describe the
concrete exploit, do not check boxes.

**Findings: 0 (SEV-1: 0, SEV-2: 0, SEV-3: 0, SEV-4: 0).** Five residual risks are
recorded below; none is a defect in the shipped behaviour, and one (licence
notices) must be closed before the first release tag.

## Scope

Source changes only. Documentation, `.claude/*.json` state and `tests/` excluded.

| file or area | state |
|---|---|
| `tools/steam_manifest_helper/SteamManifestHelper/*.cs` (the helper, C#, SteamKit2 3.4.0) | new |
| `tools/steam_manifest_helper/*/packages.lock.json`, `*.csproj` | new |
| `src/orchestrator/platform/steam/manifest_fetcher.py` | rewritten (one helper call per run) |
| `Dockerfile` (helper build stage, DepotDownloader download removed) | modified |
| `.github/workflows/ci.yml` (image limit 250 to 275 MiB) | modified |

## Deployment context that bounds exploitability

The helper runs inside the `orchestrator-agent` container on the NAS, as root
(`--user 0:0`, see the agent uid requirement), invoked by the agent's Python
process. LAN-only; its only outbound traffic is to Steam's CM servers and Steam's
CDN. The persistent mount `/depotdownloader-config` holds the Steam session and
already held DepotDownloader's token before this change. The helper has no
listening socket.

## Threat model

1. **The Steam refresh token at rest and in memory.** `session.json` holds a
   Steam refresh token that logs in as the account without a password or Steam
   Guard. An attacker with read access to the mount owns the Steam account's
   login. Mitigations: the directory is created 0700 and the file is created
   0600 (temp file opened with `UnixCreateMode` 0600, fsynced, renamed over
   `session.json`), never chmod-ed afterwards. In memory the token lives only in
   `SteamSession`, whose `ToString()` is redacted (tested). Python never receives
   it.
2. **Helper stdout as untrusted input to Python.** The helper is our own code,
   but its stdout is parsed as JSON lines and its listed manifest names are
   joined onto a path. A compromised or buggy helper (or a hostile name from
   Steam reaching a filename) must not make Python read or write outside the
   scratch directory, or crash on a stray line.
3. **The new NuGet supply chain.** A .NET SDK image and five runtime packages
   (SteamKit2 and protobuf-net, plus their transitive protobuf-net.Core 3.2.56,
   ZstdSharp.Port and System.IO.Hashing) now enter the image. A silently floated version or a
   re-tagged SDK image would change what ships.
4. **LGPL compliance.** SteamKit2 is LGPL-2.1-only. The helper is MIT. The
   library must stay a separate, replaceable assembly, with its licence text
   shipped.

## Automated

- **semgrep, Python only.** The `.semgrep/` custom rules are all
  `languages: [python]`, so the 118 files scanned are the `.py` files under `src`
  and `tools`: **0 findings**. They never looked at the C# helper. Output under
  check 5.
- **semgrep, C#.** The helper's 16 C# files (the helper and its tests), which
  handle the refresh token, were scanned separately with the registry ruleset
  `p/csharp` (27 rules): **1 finding**, adjudicated as a false positive under
  check 6. Nothing else in this audit is automated SAST for C#.
- **gitleaks** over `origin/main..HEAD`: **no leaks found**. Output under check 5.
- The helper's 46 xUnit tests run inside the image build; a failing test, or a
  test run that finds zero tests, fails the build.

## Check 1 — the token never reaches output, logs or argv

```
$ grep -nE "Write(Line)?\(.*(RefreshToken|AccessToken|Token)" tools/steam_manifest_helper/SteamManifestHelper/*.cs
(no output; grep exit 1)
$ grep -n "RefreshToken" src/orchestrator -r
(no output; grep exit 1)
```

No write call in the helper names a token, and the string `RefreshToken` does not
occur anywhere in the Python source, so Python never handles the token. The grep
only catches a direct call, so the behaviour is also pinned by tests:
`SessionStoreTests` asserts the token is absent from one exception message
(`SessionStoreTests.cs:70`) and from `SteamSession.ToString()` (`:160`); the other
`SessionImportException` texts are safe by construction (they interpolate a path,
a username or a fixed phrase, never the token), not by test. `FetchRunnerTests`
asserts the token is absent from both the JSON results and the human log. Error texts carry exception types and
HTTP status codes only, never a raw library message, so no token and no URL query
string can reach a log. The `--username` flag on argv is an account name, not a
credential. **Verdict: pass.**

## Check 2 — the subprocess is an argv list, with no shell and a timeout

```
$ grep -n "subprocess.run" -A3 src/orchestrator/platform/steam/manifest_fetcher.py
189:            proc = subprocess.run(  # noqa: S603  argv list, no shell
190-                argv, capture_output=True, text=True, timeout=self._timeout_sec
191-            )
192-        except subprocess.TimeoutExpired as e:
```

`argv` is a list built at lines 174-187 from the configured binary path, fixed
subcommand and flags, and paths under a scratch directory. No `shell=True`, so no
word-splitting or metacharacter interpretation. `timeout=` is always passed; a
hang is killed after the configured ceiling (2 h by default) and logged as
`manifest_fetch.helper_timed_out` with the stderr tail. **Verdict: pass.**

## Check 3 — manifest names cannot escape the scratch directory

```
$ grep -n "_MANIFEST_NAME_RE" src/orchestrator/platform/steam/manifest_fetcher.py
37:_MANIFEST_NAME_RE = re.compile(r"(?P<depot>\d+)_(?P<gid>\d+)\.manifest")
240:            match = _MANIFEST_NAME_RE.fullmatch(name) if isinstance(name, str) else None
```

`fullmatch` against `\d+_\d+\.manifest` admits only digits, one underscore and a
fixed suffix: no `/`, no `..`, no NUL, no leading `-`. The name is joined onto the
app directory only after the match (`manifest_fetcher.py:242`); a non-string or a
non-matching name is skipped and logged as `manifest_fetch.manifest_name_skipped`.
Test `test_a_manifest_name_outside_the_pattern_is_never_read` lists
`../../../1_2.manifest` (which a `.search` pattern would accept), plants a valid
manifest exactly where it would land, and asserts it is never read. Related hardening on the same
boundary: lines are keyed by app id with the last one winning, lines for apps not
requested are ignored, a stray non-JSON stdout line is ignored, and on exit 0 a
requested app with no line counts as failed so a run that prints only a summary
fails the job instead of reporting green. **Verdict: pass.**

## Check 4 — supply chain: exact NuGet pins, lock files, SDK digest

```
$ grep -n "Version=" tools/steam_manifest_helper/*/*.csproj
tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj:10:    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="[18.10.1]" />
tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj:11:    <PackageReference Include="xunit" Version="[2.9.3]" />
tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj:12:    <PackageReference Include="xunit.runner.visualstudio" Version="[4.0.0]" />
tools/steam_manifest_helper/SteamManifestHelper/SteamManifestHelper.csproj:14:    <PackageReference Include="SteamKit2" Version="[3.4.0]" />
tools/steam_manifest_helper/SteamManifestHelper/SteamManifestHelper.csproj:15:    <PackageReference Include="protobuf-net" Version="[3.2.56]" />
$ ls tools/steam_manifest_helper/*/packages.lock.json
tools/steam_manifest_helper/SteamManifestHelper.Tests/packages.lock.json
tools/steam_manifest_helper/SteamManifestHelper/packages.lock.json
$ grep -n "dotnet/sdk" Dockerfile
29:FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317 AS helper
```

Every `PackageReference` uses NuGet's bracket form `[x.y.z]`, which is an exact
version, not a floor. Both projects commit a `packages.lock.json`, and the image
build restores in locked mode for both runtime identifiers, so a changed or
unlisted package fails the build rather than floating. The SDK image is pinned by
digest, as the Python base image is. The test-only packages never reach the
published output. **Verdict: pass.** The lock file's `contentHash` entries do
catch a same-version content swap at restore (NU1403). What they do not catch is
a package that was already bad when the lock file was written: there is no
signature-verification policy, so the first lock is only as trustworthy as the
review of it.

## Check 5 — semgrep (Python) and gitleaks over the branch

```
$ semgrep --config .semgrep src tools 2>&1 | grep -E "Findings|Rules run|Targets scanned|^Ran"
 • Findings: 0 (0 blocking)
 • Rules run: 7
 • Targets scanned: 118
Ran 7 rules on 118 files: 0 findings.
$ gitleaks detect --no-banner --log-opts="origin/main..HEAD" 2>&1 | tail -2
2:53PM INF scanned ~305220 bytes (305.22 KB) in 129ms
2:53PM INF no leaks found
```

(The brief's `semgrep ... | tail -3` prints only semgrep's upgrade banner, so the
findings block was extracted with `grep` instead.) All 7 `.semgrep/` rules are
`languages: [python]`, so this scan covers the 118 Python files only, not the C#
helper; see check 6. gitleaks scans every file in the branch's commits, C#
included. **Verdict: pass for Python and secrets.**

## Check 6 — semgrep over the C# helper

```
$ semgrep --config p/csharp tools/steam_manifest_helper 2>&1 | tail -15
┌────────────────┐
│ 1 Code Finding │
└────────────────┘

    tools/steam_manifest_helper/SteamManifestHelper/SessionStore.cs
    ❯❱ csharp.lang.security.filesystem.unsafe-path-combine.unsafe-path-combine
          ❰❰ Blocking ❱❱
          String argument sessionDir is used to read or write data from a file via Path.Combine without direct
          sanitization via Path.GetFileName. If the path is user-supplied data this can lead to path
          traversal.
          Details: https://sg.run/1RvG

           45┆ saved = JsonSerializer.Deserialize<SteamSession>(File.ReadAllText(path));
$ semgrep --config p/csharp tools/steam_manifest_helper 2>&1 | grep -E "Findings|^Ran|Scanning 16"
  Scanning 16 files with 27 csharp rules.
 • Findings: 1 (1 blocking)
Ran 27 rules on 16 files: 1 finding.
```

**Finding: `unsafe-path-combine`, `SessionStore.cs:39-45` (`Path.Combine(sessionDir,
FileName)`). Verdict: false positive, accepted and not suppressed.** The rule
exists for a path taken from an untrusted client. Here `sessionDir` is the
`--session-dir` argument, supplied on the helper's command line by the agent's
Python process (from its own settings) or by the operator running `login`. Both
are already root in the container, so no privilege boundary is crossed, and no
network input reaches this path. The second operand, `FileName`, is a constant
(`session.json`), not caller data. Nothing is read or written outside the
operator-chosen directory by any attacker-reachable route. No code change.

## Manual review — candidates raised and why each was discarded or recorded

**1. Token file briefly world-readable.** An earlier version wrote a default-mode
(0644) temp file and chmod-ed it afterwards. Fixed during construction: the temp
file is created 0600 inside a 0700 directory and fsynced before the rename, so
the token is never readable by another user, even for an instant. **Fixed.**

**2. Reconnect storm.** A bug that looped on logon would recreate the very
problem #361 fixes. The helper makes exactly one logon attempt; on a dropped
connection or a Steam log-off it waits 60 s and reconnects once, then exits 4.
A connect failure on the first logon is `login_refused`, exit 2, with no retry.
Tested against a fake gateway. **Not exploitable.**

**3. Mid-request drop misattributed to one app.** Session loss is detected from
both the helper's own flag and the client's connected state, so a drop in the
middle of a request is retried after the reconnect instead of filed as that app's
error. Correctness, not security. **Not a finding.**

**4. Partial results archived as complete.** A failed depot makes its whole app
`error`, and Python archives `.shas` only for `ok` apps, so a partly fetched app
is retried the next week rather than recorded as complete. **Not a finding.**

**5. CDN token and URL leakage in errors.** Every error text is built from the
exception type and HTTP status only. A raw library message, which could carry a
URL query string with the CDN token, is never logged. **Not exploitable.**

**6. Wrong-owner depot key treated as "not owned".** Only `AccessDenied` is read
as "not owned"; any other refusal is an error for the app. This avoids filing a
transient Steam fault as a permanent, silent skip. **Not a finding.**

## Adjudicated: LGPL compliance

SteamKit2 3.4.0 is LGPL-2.1-only. The Dockerfile publishes the helper
`--self-contained true -p:PublishSingleFile=false -p:PublishTrimmed=false`
(`Dockerfile:42`), so `SteamKit2.dll` ships as its own replaceable file and is
not merged into the executable. The helper's own code is MIT. The full licence
texts for SteamKit2, protobuf-net (Apache-2.0, full text), ZstdSharp.Port and
System.IO.Hashing ship as `.txt` files under `tools/steam_manifest_helper/licenses/`,
because `.dockerignore` drops `*.md`. Two gaps remain and are recorded as residual
risk 5. **Verdict: the technical requirement (replaceable library) holds; the
notice requirement is incomplete until the release tag.**

## Residual risks

1. **The token sits in plain JSON on the persistent mount**, as DepotDownloader's
   did. Mode 0600 from creation, in a 0700 directory. Anyone with root on the NAS
   or the Docker volume can read it. Accepted: the previous tool had the same
   exposure, and it cannot be encrypted without a key stored beside it.
2. **`login` needs a TTY.** It reads the password from the console, so it only
   works under `docker exec -it`. It cannot be scripted or run from cron.
3. **A crash between create and rename can orphan a 0600 temp file** holding the
   token in the 0700 directory. It has the same protection as `session.json` but
   is not cleaned up automatically.
4. **Cached CDN tokens never expire within a run.** A run is bounded (hours), and
   a 403 that survives the one CDN-token retry stops the attempt, so a stale token
   fails loudly for that app rather than looping.
5. **`THIRD_PARTY_NOTICES.md` does not reach the image**, because `.dockerignore`
   drops root `*.md`. It gives no SteamKit2 source location. Licence texts for
   ZstdSharp and the .NET runtime were fetched from main or master, not the
   shipped tags. All of this is to be resolved before the first release tag.
