# Steam Manifest Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The weekly Steam manifest fetch uses one Steam login per run instead of 1,211, so it no longer rate-limits the account (#361).

**Architecture:** A new self-contained C# helper (`tools/steam_manifest_helper/`, SteamKit2 3.4.0, .NET 10) logs in once with a token imported from DepotDownloader's saved login. It fetches every selected app's Windows/64-bit manifests, writes each with `DepotManifest.SaveToFile`, and reports one JSON line per app on stdout. The existing Python fetcher keeps enumeration, manifest parsing and `.shas` writing, and now calls the helper once per run. DepotDownloader leaves the image; rollback is the `dpa-pre-361` image tag.

**Tech Stack:** C# / .NET 10 (`net10.0`), SteamKit2 3.4.0, protobuf-net 3.2.56, xUnit 2.9.3. Python 3.12, pytest. Docker multi-stage build.

**Spec:** `docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md` (approved 2026-10-07). Read it before starting.

## Global Constraints

- **Pins (exact, with lock files):**
  - SteamKit2 `[3.4.0]` and protobuf-net `[3.2.56]`.
  - Test only: Microsoft.NET.Test.Sdk `[18.10.1]`, xunit `[2.9.3]`, xunit.runner.visualstudio `[4.0.0]`.
  - Both csproj files set `RestorePackagesWithLockFile=true`, and the generated `packages.lock.json` files are committed.
- **Target framework:** `net10.0`. .NET 8 support ends November 2026.
- **SDK image:** `mcr.microsoft.com/dotnet/sdk:10.0@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317`.
- **Licensing:**
  - The helper's own code is MIT.
  - SteamKit2 is LGPL-2.1-only. Publish **self-contained but NOT single-file and NOT trimmed**, so `SteamKit2.dll` stays a separate, replaceable file.
  - License texts ship as `.txt` files: `.dockerignore` drops `*.md`.
- `InvariantGlobalization=true`: the runtime image `python:3.12-slim` has no ICU.
- **Logins:** exactly **one** Steam logon per run, and never a logon loop. After a dropped connection, wait 60 s and reconnect **once**.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | Session completed |
  | 1 | Unexpected |
  | 2 | Login refused or token rejected |
  | 3 | Session import failed |
  | 4 | Disconnected twice, or the reconnect was refused |
  | 64 | Usage error |

- **Output streams:** stdout carries **JSON lines only**: per-app `{"app","status","manifests"?,"reason"?}` plus one `{"summary":true,"session","reason"}`. stderr is the human log. Python reads both.
- **App statuses:** `ok | no_depots | not_owned | error | not_attempted`. **Session statuses:** `completed | login_refused | import_failed | disconnected`.
- **The Steam token** is never printed, logged or put on argv, and never passed to Python.
- **Depot rules:** exactly DepotDownloader 3.4.0's: `oslist` contains `windows`, `osarch == 64`, `language == english`, no `lowviolence`. Each manifest ID is `depots/<id>/manifests/public/gid`, with one `depotfromapp` redirect.
- **Manifest file name:** `<out>/<app>/<depot>_<gid>.manifest`.
- **Session file:** `<depotdownloader_config_dir>/steam-manifest-helper/session.json`, mode 0600.
- **Python interfaces:** `fetch_all()` keeps its signature, and `FetchResult(fetched, skipped, failed, apps)` stays unchanged, so the agent API does not change.
- **Agent import isolation:** `manifest_fetcher.py` stays stdlib + structlog, and must never import `orchestrator.api` or `orchestrator.db`.
- **Image size:** CI fails any amd64 image over **275 MiB** (raised from 250 by Karl, 2026-10-07, after the spike: 243.2 − 75.3 DepotDownloader + 86.4 helper = 254.3 MiB). Task 6 makes the change in `ci.yml`. If the image would exceed 275, stop and report to Karl.
- **Refinements to the spec, made while planning (1-3) and at the Task 1 gate (4-5). Task 9 records them in the spec:**
  1. `fetch` also takes `--username`, because the session and the import are keyed by account.
  2. After a reconnect, the app interrupted by the drop is **retried**, not skipped. The drop was not that app's fault.
  3. A CDN 403 fetches a CDN auth token and retries once (DepotDownloader's policy).
  4. CI's image limit is 275 MiB, not 250 (Karl, 2026-10-07). The helper is published self-contained and untrimmed, as designed, at 86.4 MiB, which puts the image at about 254 MiB.
  5. Parity is judged on chunk-SHA content (Karl, 2026-10-07). 242 archive files written in bulk on 2026-06-26 lack a trailing newline; the reader (`parse_shas`, `splitlines()`) ignores it, and the helper matches today's `_write_shas` byte for byte.
- **Hooks in this repo:**
  - Before each commit, run `bash .claude/framework/hooks/mark-evaluated.sh "reason"` as a lone command, from the repo root. The reason must contain no `;`, `&`, `|`, `>`, `<` or `$(`.
  - Stage files by name only.
  - **Execute this plan in a fresh session.** The Planning-Zone hook blocks source edits in the session that wrote the plan.

## Review Focus

1. **A depot that borrows another app's files** (`depotfromapp`, as shared redistributables do) is fetched through the parent app's info. It must not be reported `no_depots`. The test is in Task 4.
2. **An app the account doesn't own**, sitting in SteamPrefill's selection, gets `not_owned`. The run continues; this is never a session stop. Test in Task 4.
3. **A CDN that answers 403 first** gets one CDN auth token fetch and one retry, then succeeds. Test in Task 5.
4. **A stray non-JSON line on the helper's stdout** (a library debug print) is ignored by Python. It is not a crash, and not counted. Test in Task 7.
5. **A DepotDownloader store holding logins for two accounts:** the configured username's token is used, and an unknown username fails with exit 3. Tests in Task 2.

---

### Task 1: Spike — prove the approach before building it (THROWAWAY, with a STOP gate)

Nothing from this task is committed. Scratch only.

> **Result (run 2026-10-07 16:51-16:57 UTC; Karl's go given the same day).** All four questions were answered:
> 1. **Login:** `LOGON OK (logons so far: 1)`, using the imported token. No compile fixes were needed.
> 2. **Throttling:** none. `DONE apps=100 ok=100 failed=0 manifests=313 logons=1 disconnects=0`; 0 RateLimit; median ms 3101 (first 25 apps) → 2550 (last 25).
>    - 54 depot keys were `AccessDenied`. DepotDownloader never archived any of them, so they are not a regression.
>    - Two depots that were already archived were missed: one got 503 from all 3 CDN servers tried, the other had no manifest request code.
> 3. **Parity:** `identical=70 different=242`.
>    - All 242 differences are a missing trailing newline in archive files bulk-written on 2026-06-26. Their SHA sets are identical.
>    - Karl accepted this; see refinement 5.
> 4. **Size:** 86.4 MiB, which puts the image at about 254.3 MiB. Karl raised CI's limit to 275 MiB; see refinement 4.
>
> The Step 4 snippet needs a dot-file filter: `/manifest-archive/v1` holds 3,518 macOS `._*` files. Task 10 Step 5 reuses that snippet with the filter.

**Questions it must answer:**
1. Does the token imported from DepotDownloader's store log in?
2. Does Steam limit manifest requests inside one session?
3. Are the resulting `.shas` byte-identical to the archive's?
4. How big is the published helper?

**Files (scratch, outside the repo):**
- Create: `$SCRATCH/spike361/Spike361.csproj`
- Create: `$SCRATCH/spike361/Program.cs`
- Create: `$SCRATCH/spike361/parity.py`

Use `SCRATCH=$(mktemp -d)` on the Mac.

**Interfaces:**
- Consumes: nothing.
- Produces: a written report, `$SCRATCH/spike361/REPORT.txt`, and Karl's go/no-go. Tasks 2-10 do not start without it.

- [ ] **Step 1: Create the scratch project**

```bash
SCRATCH=$(mktemp -d) && cd "$SCRATCH" && dotnet new console -n Spike361 -o spike361 --framework net10.0
cd spike361 && dotnet add package SteamKit2 --version 3.4.0 && dotnet add package protobuf-net --version 3.2.56
```

Then add `<InvariantGlobalization>true</InvariantGlobalization>` inside the `<PropertyGroup>` of `Spike361.csproj`.

- [ ] **Step 2: Write `Program.cs`**

```csharp
// THROWAWAY spike for #361. Not production code.
using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using ProtoBuf;
using SteamKit2;
using SteamKit2.CDN;

if (args.Length < 4)
{
    Console.Error.WriteLine("usage: Spike361 <dd-config-dir> <username> <apps-file> <out-dir> [max-apps]");
    return 64;
}
var configDir = args[0];
var username = args[1];
var appsFile = args[2];
var outDir = args[3];
var maxApps = args.Length > 4 ? int.Parse(args[4]) : 100;

var storePath = Directory.GetFiles(configDir, "account.config", SearchOption.AllDirectories).Single();
DdStore store;
using (var fs = File.OpenRead(storePath))
using (var ds = new DeflateStream(fs, CompressionMode.Decompress))
{
    store = Serializer.Deserialize<DdStore>(ds);
}
var token = store.LoginTokens.First(kv => string.Equals(kv.Key, username, StringComparison.OrdinalIgnoreCase)).Value;
Console.Error.WriteLine($"token imported for {username} ({token.Length} chars)");

var client = new SteamClient();
var manager = new CallbackManager(client);
var user = client.GetHandler<SteamUser>()!;
var apps = client.GetHandler<SteamApps>()!;
var content = client.GetHandler<SteamContent>()!;
var connected = new TaskCompletionSource();
var loggedOn = new TaskCompletionSource<EResult>();
var logons = 0;
var disconnects = 0;
manager.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
manager.Subscribe<SteamClient.DisconnectedCallback>(cb =>
{
    disconnects++;
    Console.Error.WriteLine($"DISCONNECTED (user-initiated={cb.UserInitiated})");
    loggedOn.TrySetResult(EResult.NoConnection);
});
manager.Subscribe<SteamUser.LoggedOnCallback>(cb => loggedOn.TrySetResult(cb.Result));
using var pumpCts = new CancellationTokenSource();
var pump = Task.Run(() =>
{
    while (!pumpCts.IsCancellationRequested)
    {
        manager.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
    }
});

client.Connect();
await connected.Task.WaitAsync(TimeSpan.FromSeconds(30));
user.LogOn(new SteamUser.LogOnDetails { Username = username, AccessToken = token, ShouldRememberPassword = true });
logons++;
var logonResult = await loggedOn.Task.WaitAsync(TimeSpan.FromSeconds(30));
Console.Error.WriteLine($"LOGON {logonResult} (logons so far: {logons})");
if (logonResult != EResult.OK)
{
    return 2;
}

var appIds = File.ReadLines(appsFile).Select(l => l.Trim()).Where(l => l.Length > 0).Select(uint.Parse).Take(maxApps).ToList();
var servers = (await content.GetServersForSteamPipe())
    .Where(s => s.Type is "SteamCache" or "CDN")
    .OrderBy(s => s.WeightedLoad)
    .ToList();
Console.Error.WriteLine($"{servers.Count} CDN servers");
var cdn = new Client(client);
var infoCache = new Dictionary<uint, KeyValue?>();
int appsOk = 0, appsFailed = 0, saved = 0;
var sw = Stopwatch.StartNew();

async Task<KeyValue?> AppInfo(uint appId)
{
    if (infoCache.TryGetValue(appId, out var cached))
    {
        return cached;
    }
    var tokens = await apps.PICSGetAccessTokens([appId], []);
    var request = new SteamApps.PICSRequest(appId);
    if (tokens.AppTokens.TryGetValue(appId, out var appToken))
    {
        request.AccessToken = appToken;
    }
    var info = await apps.PICSGetProductInfo([request], []);
    KeyValue? kv = null;
    foreach (var result in info.Results ?? [])
    {
        if (result.Apps.TryGetValue(appId, out var app))
        {
            kv = app.KeyValues;
        }
    }
    infoCache[appId] = kv;
    return kv;
}

static bool Accept(KeyValue value, Func<string, bool> ok) =>
    value == KeyValue.Invalid || string.IsNullOrWhiteSpace(value.Value) || ok(value.Value!);

foreach (var appId in appIds)
{
    var appSw = Stopwatch.StartNew();
    try
    {
        var kv = await AppInfo(appId);
        if (kv is null)
        {
            Console.WriteLine($"{appId} error no-app-info");
            appsFailed++;
            continue;
        }
        var depots = kv["depots"];
        var fetchedHere = 0;
        foreach (var depot in depots.Children)
        {
            if (depot.Children.Count == 0 || !uint.TryParse(depot.Name, out var depotId))
            {
                continue;
            }
            var config = depot["config"];
            if (config != KeyValue.Invalid &&
                (!Accept(config["oslist"], v => Array.IndexOf(v.Split(','), "windows") >= 0) ||
                 !Accept(config["osarch"], v => v == "64") ||
                 !Accept(config["language"], v => v == "english") ||
                 (config["lowviolence"] != KeyValue.Invalid && config["lowviolence"].AsBoolean())))
            {
                continue;
            }

            var gidNode = depot["manifests"]["public"]["gid"];
            if (depot["manifests"] == KeyValue.Invalid && depot["depotfromapp"] != KeyValue.Invalid)
            {
                var other = depot["depotfromapp"].AsUnsignedInteger();
                var otherInfo = other == appId ? null : await AppInfo(other);
                gidNode = otherInfo is null ? KeyValue.Invalid : otherInfo["depots"][depotId.ToString()]["manifests"]["public"]["gid"];
            }
            if (gidNode.Value is null)
            {
                continue;
            }
            var gid = ulong.Parse(gidNode.Value);

            var key = await apps.GetDepotDecryptionKey(depotId, appId);
            if (key.Result != EResult.OK)
            {
                Console.Error.WriteLine($"  {appId}/{depotId} no key: {key.Result}");
                continue;
            }

            var containing = appId;
            var proxy = depot["depotfromapp"];
            if (proxy != KeyValue.Invalid && !kv["common"]["FreeToDownload"].AsBoolean())
            {
                containing = proxy.AsUnsignedInteger();
            }

            var code = await content.GetManifestRequestCode(depotId, containing, gid, "public");
            if (code == 0)
            {
                Console.Error.WriteLine($"  {appId}/{depotId} no request code");
                continue;
            }

            DepotManifest? manifest = null;
            foreach (var server in servers.Take(3))
            {
                string? cdnToken = null;
                for (var attempt = 0; attempt < 2 && manifest is null; attempt++)
                {
                    try
                    {
                        manifest = await cdn.DownloadManifestAsync(depotId, gid, code, server, key.DepotKey, null, cdnToken);
                    }
                    catch (SteamKitWebRequestException e) when (e.StatusCode == HttpStatusCode.Forbidden && cdnToken is null)
                    {
                        var auth = await content.GetCDNAuthToken(containing, depotId, server.Host!);
                        cdnToken = auth.Result == EResult.OK ? auth.Token : null;
                        if (cdnToken is null)
                        {
                            break;
                        }
                    }
                    catch (Exception e)
                    {
                        Console.Error.WriteLine($"  {appId}/{depotId} via {server.Host}: {e.GetType().Name}: {e.Message}");
                        break;
                    }
                }
                if (manifest is not null)
                {
                    break;
                }
            }
            if (manifest is null)
            {
                continue;
            }

            var dir = Path.Combine(outDir, appId.ToString());
            Directory.CreateDirectory(dir);
            manifest.SaveToFile(Path.Combine(dir, $"{depotId}_{gid}.manifest"));
            fetchedHere++;
            saved++;
        }
        Console.WriteLine($"{appId} {(fetchedHere > 0 ? "ok" : "none")} manifests={fetchedHere} ms={appSw.ElapsedMilliseconds}");
        if (fetchedHere > 0) appsOk++; else appsFailed++;
    }
    catch (Exception e)
    {
        Console.WriteLine($"{appId} error {e.GetType().Name}: {e.Message}");
        appsFailed++;
    }
}

Console.Error.WriteLine($"DONE apps={appIds.Count} ok={appsOk} failed={appsFailed} manifests={saved} " +
                        $"logons={logons} disconnects={disconnects} elapsed={sw.Elapsed}");
user.LogOff();
client.Disconnect();
pumpCts.Cancel();
await pump;
return 0;

[ProtoContract]
internal sealed class DdStore
{
    [ProtoMember(4, IsRequired = false)]
    public Dictionary<string, string> LoginTokens { get; set; } = new();
}
```

- [ ] **Step 3: Build, publish, measure**

```bash
cd "$SCRATCH/spike361" && dotnet build -c Release 2>&1 | tail -3
dotnet publish -c Release -r linux-x64 --self-contained true -p:PublishSingleFile=false -p:PublishTrimmed=false -o out
du -sh out && du -sm out
```

Expected: `Build succeeded.` Record the `du -sm` figure: it is the helper's share of the image. The current image is about 243 MiB, and removing DepotDownloader frees 76 MB, so the helper fits if it is under about 80 MiB. If the build fails, fix the compile error here, in scratch. Record every fix in `REPORT.txt`, because Task 5 ports this code.

- [ ] **Step 4: Prepare the app list (first 100 selected apps that already have `.shas`)**

```bash
ssh karl@192.168.1.30 'docker exec -i orchestrator-agent python -' <<'PY' > "$SCRATCH/spike361/apps.txt"
import json
from pathlib import Path
sel = json.loads(Path("/SteamPrefill/Config/selectedAppsToPrefill.json").read_text())
have = {int(p.name.split("_", 1)[0]) for p in Path("/manifest-archive/v1").glob("*.shas") if not p.name.startswith(".")}
for app in [a for a in sel if int(a) in have][:100]:
    print(app)
PY
wc -l "$SCRATCH/spike361/apps.txt"
```

Expected: `100`. If the path `/SteamPrefill/Config` differs, read it with `docker exec orchestrator-agent python -c "from orchestrator.core.settings import get_settings as g; print(g().steam_prefill_config_dir)"`.

- [ ] **Step 5: Pick a quiet window**

The window must avoid:
- the SteamPrefill ticks: 00:00, 06:00, 12:00 and 18:00 MDT (06, 12, 18 and 00 UTC), plus 15 minutes either side;
- the Monday `fetch_manifests` run, 05:00-13:00 UTC.

```bash
date -u +%H:%M
```

Proceed only if the time is at least 15 minutes clear of those.

- [ ] **Step 6: Copy to the NAS agent and run once**

```bash
cd "$SCRATCH/spike361" && tar czf spike.tgz out apps.txt && scp -q spike.tgz karl@192.168.1.30:/tmp/
ssh karl@192.168.1.30 'docker cp /tmp/spike.tgz orchestrator-agent:/tmp/ && docker exec -u 0 orchestrator-agent sh -c "cd /tmp && rm -rf spike361 && mkdir spike361 && tar xzf spike.tgz -C spike361"'
ssh karl@192.168.1.30 'docker exec -u 0 orchestrator-agent sh -c "/tmp/spike361/out/Spike361 /depotdownloader-config kraulerson /tmp/spike361/apps.txt /tmp/spike361/manifests 100"' > "$SCRATCH/spike361/run.stdout" 2> "$SCRATCH/spike361/run.stderr"
tail -3 "$SCRATCH/spike361/run.stderr"; sort "$SCRATCH/spike361/run.stdout" | awk '{print $2}' | sort | uniq -c
```

Expected:
- `LOGON OK (logons so far: 1)`;
- a `DONE` line showing `logons=1` and `disconnects=0`;
- most apps `ok`.

Record the per-app `ms=` spread. A rising trend suggests in-session throttling.

- [ ] **Step 7: Parity, the correctness gate**

Write `$SCRATCH/spike361/parity.py`:

```python
"""For every manifest the spike saved, compare its chunk SHAs with the .shas
DepotDownloader already archived for the same app/depot/gid."""
import sys
from pathlib import Path

from orchestrator.platform.steam.steamkit_manifest_parser import parse_steamkit_manifest

root = Path("/tmp/spike361/manifests")
archive = Path("/manifest-archive/v1")
same = differ = no_archive = 0
for path in sorted(root.glob("*/*.manifest")):
    app = path.parent.name
    depot, gid = path.stem.split("_", 1)
    shas_file = archive / f"{app}_{app}_{depot}_{gid}.shas"
    if not shas_file.exists():
        no_archive += 1
        continue
    mine = sorted(parse_steamkit_manifest(path.read_bytes()))
    theirs = shas_file.read_text().split()
    if mine == theirs:
        same += 1
    else:
        differ += 1
        print(f"DIFFER {path.name}: helper={len(mine)} archive={len(theirs)}", file=sys.stderr)
print(f"identical={same} different={differ} no_archived_copy={no_archive}")
```

```bash
scp -q "$SCRATCH/spike361/parity.py" karl@192.168.1.30:/tmp/ && ssh karl@192.168.1.30 'docker cp /tmp/parity.py orchestrator-agent:/tmp/ && docker exec orchestrator-agent python /tmp/parity.py'
```

Expected: `different=0` and `identical` greater than 0. A `no_archived_copy` count is fine: those are newer versions than the archive holds.

- [ ] **Step 8: Clean up the NAS**

```bash
ssh karl@192.168.1.30 'docker exec -u 0 orchestrator-agent rm -rf /tmp/spike361 /tmp/spike.tgz /tmp/parity.py; rm -f /tmp/spike.tgz /tmp/parity.py'
```

- [ ] **Step 9: Write `REPORT.txt`, then STOP**

Write the four answers into `$SCRATCH/spike361/REPORT.txt`, with the real output lines:
1. Did the login work, and was there exactly 1 logon?
2. Was there a rate limit or a rising `ms=` trend, and how many disconnects?
3. The parity line.
4. `du -sm` of `out`.

Also list every compile fix made in Step 3. **Report to Karl and wait for his go.** Then update this plan to match what the spike found:

| Spike result | Plan change |
|---|---|
| Login refused | Task 5 adds the `login` subcommand *first*, and Karl runs it once (phone approval) before Task 10 |
| In-session throttling seen | Add pacing between manifest requests in `FetchRunner` (Task 4) |
| Parity mismatch | STOP; do not continue |
| Over the size budget | Karl decides |

---

### Task 2: Scaffold the helper and import the saved login (`SessionStore`)

Start the build loop first:

```bash
scripts/process-checklist.sh --start-feature "steam-manifest-helper"
```

**Files:**
- Create: `tools/steam_manifest_helper/.gitignore`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/SteamManifestHelper.csproj`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/SessionStore.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/Program.cs` (placeholder entry point, replaced in Task 5)
- Create: `tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj`
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/SessionStoreTests.cs`

The test folder is deliberately **not** named `tests/`: `.dockerignore` drops a root `tests/`, and the image build must run these tests.

**Interfaces:**
- Produces:
  - `record SteamSession(string Username, string RefreshToken)`
  - `class SessionImportException(string message) : Exception`
  - `static class SessionStore` with:
    - `const string FileName = "session.json"`
    - `SteamSession Load(string sessionDir, string username, string? importFrom)`
    - `SteamSession ImportFromDepotDownloader(string configDir, string username)`
    - `void Save(string sessionDir, SteamSession session)`

- [ ] **Step 1: Project files**

`tools/steam_manifest_helper/.gitignore`:

```
bin/
obj/
```

`tools/steam_manifest_helper/SteamManifestHelper/SteamManifestHelper.csproj`:

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <AssemblyName>SteamManifestHelper</AssemblyName>
    <RootNamespace>SteamManifestHelper</RootNamespace>
    <InvariantGlobalization>true</InvariantGlobalization>
    <RestorePackagesWithLockFile>true</RestorePackagesWithLockFile>
    <RuntimeIdentifiers>linux-x64;linux-arm64</RuntimeIdentifiers>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="SteamKit2" Version="[3.4.0]" />
    <PackageReference Include="protobuf-net" Version="[3.2.56]" />
  </ItemGroup>
  <ItemGroup>
    <InternalsVisibleTo Include="SteamManifestHelper.Tests" />
  </ItemGroup>
</Project>
```

`tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj`:

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <IsPackable>false</IsPackable>
    <RestorePackagesWithLockFile>true</RestorePackagesWithLockFile>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="[18.10.1]" />
    <PackageReference Include="xunit" Version="[2.9.3]" />
    <PackageReference Include="xunit.runner.visualstudio" Version="[4.0.0]" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="../SteamManifestHelper/SteamManifestHelper.csproj" />
  </ItemGroup>
</Project>
```

`tools/steam_manifest_helper/SteamManifestHelper/Program.cs` (replaced in Task 5):

```csharp
return 64;
```

- [ ] **Step 2: Write the failing tests**

`tools/steam_manifest_helper/SteamManifestHelper.Tests/SessionStoreTests.cs`:

```csharp
using System.IO.Compression;
using ProtoBuf;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

// What DepotDownloader 3.4.0 really writes: fields 2, 4 and 5. Fields 2 and 5
// must be skipped by our reader, not trip it.
[ProtoContract]
internal sealed class FullDepotDownloaderStore
{
    [ProtoMember(2)] public Dictionary<string, int> ContentServerPenalty { get; set; } = new();
    [ProtoMember(4)] public Dictionary<string, string> LoginTokens { get; set; } = new();
    [ProtoMember(5)] public Dictionary<string, string> GuardData { get; set; } = new();
}

public sealed class SessionStoreTests : IDisposable
{
    private const string Token = "eyJ-test-refresh-token-0123456789";
    private readonly string root = Directory.CreateTempSubdirectory("smh-session-").FullName;

    public void Dispose() => Directory.Delete(root, recursive: true);

    private string WriteStore(Dictionary<string, string> tokens)
    {
        var configDir = Path.Combine(root, "dd");
        var path = Path.Combine(configDir, ".local", "share", "IsolatedStorage", "aa", "bb", "AssemFiles", "account.config");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var file = File.Create(path);
        using var deflate = new DeflateStream(file, CompressionMode.Compress);
        Serializer.Serialize(deflate, new FullDepotDownloaderStore
        {
            ContentServerPenalty = { ["cache1.example"] = 3 },
            LoginTokens = tokens,
            GuardData = { ["kraulerson"] = "guard" },
        });
        return configDir;
    }

    [Fact]
    public void Imports_the_token_for_the_configured_account()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var session = SessionStore.ImportFromDepotDownloader(dd, "kraulerson");
        Assert.Equal("kraulerson", session.Username);
        Assert.Equal(Token, session.RefreshToken);
    }

    [Fact]
    public void Username_match_ignores_case()
    {
        var dd = WriteStore(new() { ["KRaulerson"] = Token });
        Assert.Equal(Token, SessionStore.ImportFromDepotDownloader(dd, "kraulerson").RefreshToken);
    }

    [Fact]
    public void Picks_the_configured_account_when_the_store_holds_two()
    {
        var dd = WriteStore(new() { ["someoneelse"] = "other-token", ["kraulerson"] = Token });
        Assert.Equal(Token, SessionStore.ImportFromDepotDownloader(dd, "kraulerson").RefreshToken);
    }

    [Fact]
    public void An_unknown_account_fails_without_revealing_any_token()
    {
        var dd = WriteStore(new() { ["someoneelse"] = Token });
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(dd, "kraulerson"));
        Assert.Contains("kraulerson", e.Message);
        Assert.DoesNotContain(Token, e.Message);
    }

    [Fact]
    public void A_missing_store_fails_clearly()
    {
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(Path.Combine(root, "nope"), "kraulerson"));
        Assert.Contains("account.config", e.Message);
    }

    [Fact]
    public void A_corrupt_store_fails_clearly()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var path = Directory.GetFiles(dd, "account.config", SearchOption.AllDirectories).Single();
        File.WriteAllBytes(path, [0xFF, 0xFE, 0x00, 0x13, 0x37]);
        var e = Assert.Throws<SessionImportException>(() => SessionStore.ImportFromDepotDownloader(dd, "kraulerson"));
        Assert.Contains("cannot read", e.Message);
    }

    [Fact]
    public void Load_imports_once_then_reuses_its_own_copy()
    {
        var dd = WriteStore(new() { ["kraulerson"] = Token });
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Load(sessionDir, "kraulerson", dd);
        Directory.Delete(dd, recursive: true);
        Assert.Equal(Token, SessionStore.Load(sessionDir, "kraulerson", dd).RefreshToken);
    }

    [Fact]
    public void The_saved_session_is_readable_only_by_its_owner()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", Token));
        var mode = File.GetUnixFileMode(Path.Combine(sessionDir, SessionStore.FileName));
        Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, mode);
    }

    [Fact]
    public void A_saved_session_for_another_account_is_refused()
    {
        var sessionDir = Path.Combine(root, "helper");
        SessionStore.Save(sessionDir, new SteamSession("someoneelse", Token));
        Assert.Throws<SessionImportException>(() => SessionStore.Load(sessionDir, "kraulerson", null));
    }

    [Fact]
    public void An_empty_username_names_the_setting_to_fix()
    {
        var e = Assert.Throws<SessionImportException>(() => SessionStore.Load(Path.Combine(root, "helper"), "", null));
        Assert.Contains("ORCH_STEAM_USERNAME", e.Message);
    }

    [Fact]
    public void A_session_never_prints_its_token()
    {
        Assert.DoesNotContain(Token, new SteamSession("kraulerson", Token).ToString());
    }
}
```

- [ ] **Step 3: Run them to verify they fail**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -5
```

Expected: a build error, `The type or namespace name 'SessionStore' could not be found`. Then mark the step:

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written && scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```

- [ ] **Step 4: Implement `SessionStore.cs`**

`tools/steam_manifest_helper/SteamManifestHelper/SessionStore.cs`:

```csharp
using System.IO.Compression;
using System.Text.Json;
using ProtoBuf;

namespace SteamManifestHelper;

/// <summary>The one credential the helper holds: a Steam refresh token for one account.</summary>
public sealed record SteamSession(string Username, string RefreshToken)
{
    // A record's generated ToString prints every property. Never the token.
    public override string ToString() => $"SteamSession {{ Username = {Username} }}";
}

/// <summary>No usable session could be loaded or imported (exit code 3).</summary>
public sealed class SessionImportException(string message) : Exception(message);

/// <summary>Mirror of DepotDownloader 3.4.0's AccountSettingsStore. Only field 4,
/// LoginTokens (username to refresh token), is read; protobuf skips the rest.</summary>
[ProtoContract]
internal sealed class DepotDownloaderAccountStore
{
    [ProtoMember(4, IsRequired = false)]
    public Dictionary<string, string> LoginTokens { get; set; } = new();
}

public static class SessionStore
{
    public const string FileName = "session.json";

    /// <summary>Load this helper's own session, importing it once from
    /// DepotDownloader's store when none exists yet.</summary>
    public static SteamSession Load(string sessionDir, string username, string? importFrom)
    {
        if (string.IsNullOrWhiteSpace(username))
        {
            throw new SessionImportException("no Steam username given (set ORCH_STEAM_USERNAME)");
        }

        var path = Path.Combine(sessionDir, FileName);
        if (File.Exists(path))
        {
            SteamSession? saved;
            try
            {
                saved = JsonSerializer.Deserialize<SteamSession>(File.ReadAllText(path));
            }
            catch (JsonException)
            {
                throw new SessionImportException($"{path} is not a valid session file");
            }
            if (saved is null || string.IsNullOrEmpty(saved.RefreshToken))
            {
                throw new SessionImportException($"{path} holds no login token");
            }
            if (!string.Equals(saved.Username, username, StringComparison.OrdinalIgnoreCase))
            {
                throw new SessionImportException($"{path} holds a login for a different account");
            }
            return saved;
        }

        if (importFrom is null)
        {
            throw new SessionImportException($"no session at {path} and no --import-from directory");
        }
        var imported = ImportFromDepotDownloader(importFrom, username);
        Save(sessionDir, imported);
        return imported;
    }

    public static SteamSession ImportFromDepotDownloader(string configDir, string username)
    {
        var stores = Directory.Exists(configDir)
            ? Directory.GetFiles(configDir, "account.config", SearchOption.AllDirectories)
            : [];
        if (stores.Length == 0)
        {
            throw new SessionImportException($"no DepotDownloader account.config under {configDir}");
        }

        foreach (var store in stores)
        {
            DepotDownloaderAccountStore parsed;
            try
            {
                parsed = ReadStore(store);
            }
            catch (Exception e) when (e is InvalidDataException or ProtoException or IOException)
            {
                throw new SessionImportException($"cannot read {store}: {e.GetType().Name}");
            }
            foreach (var (account, token) in parsed.LoginTokens)
            {
                if (string.Equals(account, username, StringComparison.OrdinalIgnoreCase) && token.Length > 0)
                {
                    return new SteamSession(account, token);
                }
            }
        }
        throw new SessionImportException($"DepotDownloader holds no saved login for account '{username}'");
    }

    internal static DepotDownloaderAccountStore ReadStore(string path)
    {
        using var file = File.OpenRead(path);
        using var inflate = new DeflateStream(file, CompressionMode.Decompress);
        return Serializer.Deserialize<DepotDownloaderAccountStore>(inflate);
    }

    public static void Save(string sessionDir, SteamSession session)
    {
        Directory.CreateDirectory(sessionDir);
        var path = Path.Combine(sessionDir, FileName);
        var temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(session));
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(temp, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
        File.Move(temp, path, overwrite: true);
    }
}
```

- [ ] **Step 5: Run the tests to verify they pass, then generate the lock files**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
ls SteamManifestHelper/packages.lock.json SteamManifestHelper.Tests/packages.lock.json
```

Expected: `Passed!  - Failed: 0, Passed: 11`, and both lock files listed. If `A_corrupt_store_fails_clearly` sees an exception type not in the filter, add that type to the `when` clause. Do not catch `Exception` broadly.

- [ ] **Step 6: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 2 of the approved 361 plan: scaffold the helper and the saved-login import, test-first"
```

```bash
git add tools/steam_manifest_helper/.gitignore tools/steam_manifest_helper/SteamManifestHelper/SteamManifestHelper.csproj tools/steam_manifest_helper/SteamManifestHelper/SessionStore.cs tools/steam_manifest_helper/SteamManifestHelper/Program.cs tools/steam_manifest_helper/SteamManifestHelper/packages.lock.json tools/steam_manifest_helper/SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj tools/steam_manifest_helper/SteamManifestHelper.Tests/SessionStoreTests.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/packages.lock.json
git status --short
git commit -m "feat(steam): helper scaffold and DepotDownloader login import (#361)"
```

---

### Task 3: Choose depots exactly as DepotDownloader does (`DepotSelector`)

**Files:**
- Create: `tools/steam_manifest_helper/SteamManifestHelper/DepotSelector.cs`
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/DepotSelectorTests.cs`

**Interfaces:**
- Consumes: SteamKit2 `KeyValue` (`KeyValue.LoadFromString`, `KeyValue.Invalid`, indexer, `AsBoolean`, `AsUnsignedInteger`).
- Produces:
  - `readonly record struct ManifestLookup(ulong ManifestId, uint RedirectAppId)`
  - `static class DepotSelector` with:
    - `IReadOnlyList<uint> SelectDepots(KeyValue depots, string os = "windows", string arch = "64", string language = "english")`
    - `ManifestLookup ResolveManifest(KeyValue depots, uint depotId, uint appId, string branch = "public")`
    - `uint ContainingAppId(KeyValue appInfo, uint depotId, uint appId)`

- [ ] **Step 1: Write the failing tests**

`tools/steam_manifest_helper/SteamManifestHelper.Tests/DepotSelectorTests.cs`:

```csharp
using SteamKit2;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class DepotSelectorTests
{
    private static KeyValue Depots(string body) => KeyValue.LoadFromString($"\"depots\"\n{{\n{body}\n}}")!;

    private static KeyValue App(string depotsBody, string common = "") =>
        KeyValue.LoadFromString($"\"appinfo\"\n{{\n\"common\"\n{{\n{common}\n}}\n\"depots\"\n{{\n{depotsBody}\n}}\n}}")!;

    [Fact]
    public void Keeps_windows_64_bit_english_depots_and_drops_the_rest()
    {
        var depots = Depots("""
            "731" { "config" { "oslist" "windows" "osarch" "64" } "manifests" { "public" { "gid" "1" } } }
            "732" { "config" { "oslist" "linux" } "manifests" { "public" { "gid" "2" } } }
            "733" { "config" { "oslist" "macos" } "manifests" { "public" { "gid" "3" } } }
            "734" { "config" { "oslist" "windows,macos" } "manifests" { "public" { "gid" "4" } } }
            "735" { "config" { "oslist" "windows" "osarch" "32" } "manifests" { "public" { "gid" "5" } } }
            "736" { "config" { "language" "german" } "manifests" { "public" { "gid" "6" } } }
            "737" { "config" { "lowviolence" "1" } "manifests" { "public" { "gid" "7" } } }
            "738" { "manifests" { "public" { "gid" "8" } } }
            "branches" { "public" { "buildid" "99" } }
            "baselanguages" "english"
            """);
        Assert.Equal(new uint[] { 731, 734, 738 }, DepotSelector.SelectDepots(depots));
    }

    [Fact]
    public void A_blank_setting_restricts_nothing()
    {
        var depots = Depots("""
            "731" { "config" { "oslist" "" "osarch" "" "language" "" } "manifests" { "public" { "gid" "1" } } }
            """);
        Assert.Equal(new uint[] { 731 }, DepotSelector.SelectDepots(depots));
    }

    [Fact]
    public void Resolves_the_public_manifest_id()
    {
        var depots = Depots("""
            "731" { "manifests" { "public" { "gid" "7617088375292372759" } } }
            """);
        Assert.Equal(new ManifestLookup(7617088375292372759, 0), DepotSelector.ResolveManifest(depots, 731, 730));
    }

    [Fact]
    public void A_depot_with_no_public_manifest_resolves_to_nothing()
    {
        var depots = Depots("""
            "731" { "manifests" { "beta" { "gid" "5" } } }
            "732" { "config" { "oslist" "windows" } }
            """);
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 731, 730));
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 732, 730));
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 999, 730));
    }

    [Fact]
    public void A_borrowed_depot_redirects_to_the_app_that_owns_it()
    {
        var depots = Depots("""
            "228988" { "depotfromapp" "228980" }
            """);
        Assert.Equal(new ManifestLookup(0, 228980), DepotSelector.ResolveManifest(depots, 228988, 730));
    }

    [Fact]
    public void A_depot_borrowing_from_itself_resolves_to_nothing()
    {
        var depots = Depots("""
            "731" { "depotfromapp" "730" }
            """);
        Assert.Equal(default, DepotSelector.ResolveManifest(depots, 731, 730));
    }

    [Fact]
    public void A_borrowed_depot_is_requested_through_its_owner_unless_the_app_is_free()
    {
        var paid = App("""
            "228988" { "depotfromapp" "228980" }
            """);
        var free = App("""
            "228988" { "depotfromapp" "228980" }
            """, common: "\"FreeToDownload\" \"1\"");
        Assert.Equal(228980u, DepotSelector.ContainingAppId(paid, 228988, 730));
        Assert.Equal(730u, DepotSelector.ContainingAppId(free, 228988, 730));
        Assert.Equal(730u, DepotSelector.ContainingAppId(paid, 731, 730));
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
```

Expected: a build error, `'DepotSelector' could not be found`.

- [ ] **Step 3: Implement `DepotSelector.cs`**

```csharp
using SteamKit2;

namespace SteamManifestHelper;

/// <summary>Where a depot's manifest id lives. RedirectAppId != 0 means the depot
/// borrows another app's files ("depotfromapp") and the id is in that app's info.</summary>
public readonly record struct ManifestLookup(ulong ManifestId, uint RedirectAppId);

/// <summary>DepotDownloader 3.4.0's depot rules for "-os windows -osarch 64", with
/// its defaults: English, no low-violence variants (ContentDownloader.cs:463-515,
/// GetSteam3DepotManifest at :206, GetDepotInfo at :558).</summary>
public static class DepotSelector
{
    public static IReadOnlyList<uint> SelectDepots(KeyValue depots, string os = "windows", string arch = "64", string language = "english")
    {
        var selected = new List<uint>();
        foreach (var depot in depots.Children)
        {
            if (depot.Children.Count == 0 || !uint.TryParse(depot.Name, out var depotId))
            {
                continue;
            }
            var config = depot["config"];
            if (config != KeyValue.Invalid &&
                (!Allows(config["oslist"], v => Array.IndexOf(v.Split(','), os) >= 0) ||
                 !Allows(config["osarch"], v => v == arch) ||
                 !Allows(config["language"], v => v == language) ||
                 (config["lowviolence"] != KeyValue.Invalid && config["lowviolence"].AsBoolean())))
            {
                continue;
            }
            selected.Add(depotId);
        }
        return selected;
    }

    // An absent or blank setting restricts nothing, as in DepotDownloader.
    private static bool Allows(KeyValue setting, Func<string, bool> accepts) =>
        setting == KeyValue.Invalid || string.IsNullOrWhiteSpace(setting.Value) || accepts(setting.Value);

    public static ManifestLookup ResolveManifest(KeyValue depots, uint depotId, uint appId, string branch = "public")
    {
        var depot = depots[depotId.ToString()];
        if (depot == KeyValue.Invalid)
        {
            return default;
        }
        if (depot["manifests"] == KeyValue.Invalid && depot["depotfromapp"] != KeyValue.Invalid)
        {
            var other = depot["depotfromapp"].AsUnsignedInteger();
            return other == appId ? default : new ManifestLookup(0, other);
        }
        return ulong.TryParse(depot["manifests"][branch]["gid"].Value, out var manifestId)
            ? new ManifestLookup(manifestId, 0)
            : default;
    }

    public static uint ContainingAppId(KeyValue appInfo, uint depotId, uint appId)
    {
        var proxy = appInfo["depots"][depotId.ToString()]["depotfromapp"];
        if (proxy == KeyValue.Invalid || appInfo["common"]["FreeToDownload"].AsBoolean())
        {
            return appId;
        }
        var proxyAppId = proxy.AsUnsignedInteger();
        return proxyAppId == 0 ? appId : proxyAppId;
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
```

Expected: `Passed!  - Failed: 0, Passed: 18`.

- [ ] **Step 5: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 3 of the approved 361 plan: depot selection matching DepotDownloader, test-first"
```

```bash
git add tools/steam_manifest_helper/SteamManifestHelper/DepotSelector.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/DepotSelectorTests.cs
git status --short
git commit -m "feat(steam): helper depot selection matching DepotDownloader 3.4.0 (#361)"
```

---

### Task 4: One login, per-app results, one reconnect (`FetchRunner`)

**Files:**
- Create: `tools/steam_manifest_helper/SteamManifestHelper/ISteamGateway.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/Results.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/FetchRunner.cs`
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/FakeSteamGateway.cs`
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/FetchRunnerTests.cs`

**Interfaces:**
- Consumes:
  - `SteamSession` (Task 2).
  - `DepotSelector`, `ManifestLookup` (Task 3).
- Produces:
  - `enum LogOnOutcome { Ok, TokenRejected, Refused }`
  - `record LogOnResult(LogOnOutcome Outcome, string Detail)`
  - `class SessionLostException(string)`, for a dropped connection.
  - `class SteamRequestException(string)`, for one failed request while the session is still up.
  - `interface ISteamGateway : IAsyncDisposable` with:
    - `Task<LogOnResult> ConnectAndLogOnAsync(SteamSession, CancellationToken)`
    - `Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken)`
    - `Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken)`
    - `Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken)`
  - `static class ExitCodes { Completed=0, Unexpected=1, LoginRefused=2, ImportFailed=3, Disconnected=4, Usage=64 }`
  - `static class AppStatus` (string constants).
  - `static class SessionStatus` (string constants).
  - `record AppResult(uint App, string Status, IReadOnlyList<string>? Manifests = null, string? Reason = null)`
  - `record RunSummary(string Session, string Reason)`, serialized with `"summary": true`.
  - `static class ResultWriter` with `Write(TextWriter, AppResult)` and `Write(TextWriter, RunSummary)`.
  - `class FetchRunner(ISteamGateway gateway, TextWriter results, TextWriter log, Func<TimeSpan, CancellationToken, Task> delay)` with:
    - `static readonly TimeSpan ReconnectWait = 60 s`
    - `Task<int> RunAsync(SteamSession session, IReadOnlyList<uint> appIds, string outDir, CancellationToken ct)`

- [ ] **Step 1: Write the interface and result types (no behaviour to test yet)**

`ISteamGateway.cs`:

```csharp
using SteamKit2;

namespace SteamManifestHelper;

public enum LogOnOutcome { Ok, TokenRejected, Refused }

public sealed record LogOnResult(LogOnOutcome Outcome, string Detail);

/// <summary>The connection to Steam dropped. Only FetchRunner handles it.</summary>
public sealed class SessionLostException(string message) : Exception(message);

/// <summary>One request failed while the session is still up (no request code,
/// CDN 401/404, a timeout...). The app it belongs to is reported as an error.</summary>
public sealed class SteamRequestException(string message) : Exception(message);

public interface ISteamGateway : IAsyncDisposable
{
    /// <summary>Connect, and log on ONCE with the session's token. Never retries.</summary>
    Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct);

    /// <summary>The app's PICS key/values, or null when Steam returns none.</summary>
    Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct);

    /// <summary>The depot decryption key, or null when this account has no access.</summary>
    Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct);

    /// <summary>Download one manifest and write it with DepotManifest.SaveToFile.</summary>
    Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct);
}
```

`Results.cs`:

```csharp
using System.Text.Json;
using System.Text.Json.Serialization;

namespace SteamManifestHelper;

public static class ExitCodes
{
    public const int Completed = 0;
    public const int Unexpected = 1;
    public const int LoginRefused = 2;
    public const int ImportFailed = 3;
    public const int Disconnected = 4;
    public const int Usage = 64;
}

public static class AppStatus
{
    public const string Ok = "ok";
    public const string NoDepots = "no_depots";
    public const string NotOwned = "not_owned";
    public const string Error = "error";
    public const string NotAttempted = "not_attempted";
}

public static class SessionStatus
{
    public const string Completed = "completed";
    public const string LoginRefused = "login_refused";
    public const string ImportFailed = "import_failed";
    public const string Disconnected = "disconnected";
}

public sealed record AppResult(uint App, string Status, IReadOnlyList<string>? Manifests = null, string? Reason = null);

public sealed record RunSummary(string Session, string Reason)
{
    public bool Summary => true;
}

/// <summary>stdout is a JSON-lines channel for Python. Nothing else may write to it.</summary>
public static class ResultWriter
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public static void Write(TextWriter output, AppResult line) => Emit(output, JsonSerializer.Serialize(line, Options));

    public static void Write(TextWriter output, RunSummary line) => Emit(output, JsonSerializer.Serialize(line, Options));

    private static void Emit(TextWriter output, string json)
    {
        output.WriteLine(json);
        output.Flush();
    }
}
```

- [ ] **Step 2: Write the fake gateway and the failing runner tests**

`FakeSteamGateway.cs`:

```csharp
using SteamKit2;
using SteamManifestHelper;

namespace SteamManifestHelper.Tests;

internal sealed class FakeSteamGateway : ISteamGateway
{
    public readonly Queue<LogOnResult> LogOnResults = new();
    public readonly Dictionary<uint, KeyValue?> Apps = new();
    public readonly HashSet<uint> DepotsWithoutAccess = new();
    public readonly HashSet<uint> UnavailableDepots = new();
    /// <summary>app id -> how many more times asking for its info drops the connection.</summary>
    public readonly Dictionary<uint, int> DropsOnApp = new();
    public readonly List<(uint Depot, uint ContainingApp, ulong Manifest)> Saved = new();
    public int LogOnCalls { get; private set; }

    public Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct)
    {
        LogOnCalls++;
        return Task.FromResult(LogOnResults.Count > 0 ? LogOnResults.Dequeue() : new LogOnResult(LogOnOutcome.Ok, "OK"));
    }

    public Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct)
    {
        if (DropsOnApp.TryGetValue(appId, out var drops) && drops > 0)
        {
            DropsOnApp[appId] = drops - 1;
            throw new SessionLostException("NoConnection");
        }
        return Task.FromResult(Apps.TryGetValue(appId, out var info) ? info : null);
    }

    public Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct) =>
        Task.FromResult<byte[]?>(DepotsWithoutAccess.Contains(depotId) ? null : [1, 2, 3]);

    public Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct)
    {
        if (UnavailableDepots.Contains(depotId))
        {
            throw new SteamRequestException("CDN returned 404");
        }
        Saved.Add((depotId, containingAppId, manifestId));
        File.WriteAllBytes(path, [0xD0, 0x17, 0xF6, 0x71]);
        return Task.CompletedTask;
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
```

`FetchRunnerTests.cs`:

```csharp
using System.Text.Json;
using SteamKit2;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class FetchRunnerTests : IDisposable
{
    private const string Token = "eyJ-test-refresh-token-0123456789";
    private static readonly SteamSession Session = new("kraulerson", Token);
    private readonly string outDir = Directory.CreateTempSubdirectory("smh-out-").FullName;
    private readonly StringWriter results = new();
    private readonly StringWriter log = new();
    private readonly List<TimeSpan> delays = new();

    public void Dispose() => Directory.Delete(outDir, recursive: true);

    private static KeyValue App(string depotsBody, string common = "") =>
        KeyValue.LoadFromString($"\"appinfo\"\n{{\n\"common\"\n{{\n{common}\n}}\n\"depots\"\n{{\n{depotsBody}\n}}\n}}")!;

    private static string Depot(uint id, ulong gid) =>
        $"\"{id}\" {{ \"config\" {{ \"oslist\" \"windows\" }} \"manifests\" {{ \"public\" {{ \"gid\" \"{gid}\" }} }} }}";

    private FetchRunner Runner(FakeSteamGateway gateway) =>
        new(gateway, results, log, (wait, _) => { delays.Add(wait); return Task.CompletedTask; });

    private List<JsonElement> Lines() =>
        results.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Select(l => JsonDocument.Parse(l).RootElement).ToList();

    private static FakeSteamGateway ThreeGoodApps()
    {
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App(Depot(11, 111));
        gateway.Apps[20] = App(Depot(21, 222));
        gateway.Apps[30] = App(Depot(31, 333));
        return gateway;
    }

    [Fact]
    public async Task Logs_on_once_for_the_whole_run()
    {
        var gateway = ThreeGoodApps();
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(1, gateway.LogOnCalls);
        var lines = Lines();
        Assert.Equal(new[] { "ok", "ok", "ok" }, lines.Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal("11_111.manifest", lines[0].GetProperty("manifests")[0].GetString());
        Assert.True(File.Exists(Path.Combine(outDir, "10", "11_111.manifest")));
        Assert.True(lines[^1].GetProperty("summary").GetBoolean());
        Assert.Equal("completed", lines[^1].GetProperty("session").GetString());
    }

    [Fact]
    public async Task A_refused_login_stops_at_once_with_steams_reason()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Refused, "RateLimitExceeded"));
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.LoginRefused, code);
        Assert.Equal(1, gateway.LogOnCalls);
        var only = Assert.Single(Lines());
        Assert.Equal("login_refused", only.GetProperty("session").GetString());
        Assert.Contains("RateLimitExceeded", only.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task A_rejected_token_names_the_login_command()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.TokenRejected, "Expired"));
        var code = await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.LoginRefused, code);
        Assert.Contains("login --username kraulerson", Lines().Single().GetProperty("reason").GetString());
    }

    [Fact]
    public async Task One_drop_reconnects_once_and_retries_the_interrupted_app()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 1;
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(2, gateway.LogOnCalls);
        Assert.Equal(new[] { FetchRunner.ReconnectWait }, delays);
        Assert.Equal(new[] { "ok", "ok", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
    }

    [Fact]
    public async Task A_second_drop_stops_and_marks_the_rest_not_attempted()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 2;
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Disconnected, code);
        Assert.Equal(2, gateway.LogOnCalls);
        var lines = Lines();
        Assert.Equal(new[] { "ok", "not_attempted", "not_attempted" }, lines.Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal("disconnected", lines[^1].GetProperty("session").GetString());
    }

    [Fact]
    public async Task A_refused_reconnect_stops_as_disconnected()
    {
        var gateway = ThreeGoodApps();
        gateway.DropsOnApp[20] = 1;
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Ok, "OK"));
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.Refused, "RateLimitExceeded"));
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Disconnected, code);
        Assert.Contains("RateLimitExceeded", Lines()[^1].GetProperty("reason").GetString());
    }

    [Fact]
    public async Task An_app_steam_knows_nothing_about_is_an_error_and_the_run_continues()
    {
        var gateway = ThreeGoodApps();
        gateway.Apps.Remove(20);
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(new[] { "ok", "error", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
    }

    [Fact]
    public async Task An_app_the_account_does_not_own_is_not_owned_and_the_run_continues()
    {
        // Review Focus 2.
        var gateway = ThreeGoodApps();
        gateway.DepotsWithoutAccess.Add(21);
        var code = await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(new[] { "ok", "not_owned", "ok" }, Lines().Take(3).Select(l => l.GetProperty("status").GetString()));
        Assert.Equal(1, gateway.LogOnCalls);
    }

    [Fact]
    public async Task An_app_with_no_windows_depot_is_no_depots()
    {
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App("\"11\" { \"config\" { \"oslist\" \"linux\" } \"manifests\" { \"public\" { \"gid\" \"1\" } } }");
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.Equal("no_depots", Lines()[0].GetProperty("status").GetString());
    }

    [Fact]
    public async Task A_borrowed_depot_is_fetched_through_the_owning_app()
    {
        // Review Focus 1.
        var gateway = new FakeSteamGateway();
        gateway.Apps[10] = App("\"228988\" { \"depotfromapp\" \"228980\" }");
        gateway.Apps[228980] = App(Depot(228988, 4242));
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        var line = Lines()[0];
        Assert.Equal("ok", line.GetProperty("status").GetString());
        Assert.Equal("228988_4242.manifest", line.GetProperty("manifests")[0].GetString());
        Assert.Equal((228988u, 228980u, 4242ul), gateway.Saved.Single());
    }

    [Fact]
    public async Task An_unavailable_manifest_is_an_error_with_its_reason()
    {
        var gateway = ThreeGoodApps();
        gateway.UnavailableDepots.Add(21);
        await Runner(gateway).RunAsync(Session, [10, 20, 30], outDir, CancellationToken.None);
        var failed = Lines()[1];
        Assert.Equal("error", failed.GetProperty("status").GetString());
        Assert.Contains("404", failed.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task The_token_appears_in_no_output()
    {
        var gateway = ThreeGoodApps();
        gateway.LogOnResults.Enqueue(new LogOnResult(LogOnOutcome.TokenRejected, "Expired"));
        await Runner(gateway).RunAsync(Session, [10], outDir, CancellationToken.None);
        Assert.DoesNotContain(Token, results.ToString());
        Assert.DoesNotContain(Token, log.ToString());
    }
}
```

- [ ] **Step 3: Run them to verify they fail**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
```

Expected: a build error, `'FetchRunner' could not be found`.

- [ ] **Step 4: Implement `FetchRunner.cs`**

```csharp
namespace SteamManifestHelper;

/// <summary>One login for the whole run, one JSON line per app, and at most one
/// reconnect. A logon loop is what #361 was; this class must never make one.</summary>
public sealed class FetchRunner(ISteamGateway gateway, TextWriter results, TextWriter log, Func<TimeSpan, CancellationToken, Task> delay)
{
    public static readonly TimeSpan ReconnectWait = TimeSpan.FromSeconds(60);

    public async Task<int> RunAsync(SteamSession session, IReadOnlyList<uint> appIds, string outDir, CancellationToken ct)
    {
        var logon = await gateway.ConnectAndLogOnAsync(session, ct);
        if (logon.Outcome != LogOnOutcome.Ok)
        {
            var reason = logon.Outcome == LogOnOutcome.TokenRejected
                ? $"Steam rejected the saved login ({logon.Detail}). Run: SteamManifestHelper login --username {session.Username} --session-dir <session dir>"
                : $"Steam refused the login: {logon.Detail}";
            log.WriteLine(reason);
            ResultWriter.Write(results, new RunSummary(SessionStatus.LoginRefused, reason));
            return ExitCodes.LoginRefused;
        }
        log.WriteLine($"logged on as {session.Username}; fetching {appIds.Count} apps");

        var reconnected = false;
        var index = 0;
        while (index < appIds.Count)
        {
            try
            {
                ResultWriter.Write(results, await FetchAppAsync(appIds[index], outDir, ct));
                index++;
            }
            catch (SessionLostException lost)
            {
                log.WriteLine($"connection lost at app {appIds[index]}: {lost.Message}");
                if (reconnected)
                {
                    return StopEarly(appIds, index, $"connection lost twice: {lost.Message}");
                }
                reconnected = true;
                await delay(ReconnectWait, ct);
                var again = await gateway.ConnectAndLogOnAsync(session, ct);
                if (again.Outcome != LogOnOutcome.Ok)
                {
                    return StopEarly(appIds, index, $"reconnect refused: {again.Detail}");
                }
                log.WriteLine("reconnected; retrying the interrupted app");
            }
        }
        ResultWriter.Write(results, new RunSummary(SessionStatus.Completed, ""));
        return ExitCodes.Completed;
    }

    private int StopEarly(IReadOnlyList<uint> appIds, int from, string reason)
    {
        for (var i = from; i < appIds.Count; i++)
        {
            ResultWriter.Write(results, new AppResult(appIds[i], AppStatus.NotAttempted, Reason: reason));
        }
        log.WriteLine(reason);
        ResultWriter.Write(results, new RunSummary(SessionStatus.Disconnected, reason));
        return ExitCodes.Disconnected;
    }

    private async Task<AppResult> FetchAppAsync(uint appId, string outDir, CancellationToken ct)
    {
        try
        {
            var info = await gateway.GetAppInfoAsync(appId, ct);
            if (info is null)
            {
                return new AppResult(appId, AppStatus.Error, Reason: "Steam returned no app info");
            }
            var depots = info["depots"];
            var selected = DepotSelector.SelectDepots(depots);
            if (selected.Count == 0)
            {
                return new AppResult(appId, AppStatus.NoDepots, Reason: "no Windows 64-bit English depots");
            }

            var saved = new List<string>();
            var noAccess = 0;
            foreach (var depotId in selected)
            {
                var lookup = DepotSelector.ResolveManifest(depots, depotId, appId);
                if (lookup.RedirectAppId != 0)
                {
                    var owner = await gateway.GetAppInfoAsync(lookup.RedirectAppId, ct);
                    lookup = owner is null ? default : DepotSelector.ResolveManifest(owner["depots"], depotId, lookup.RedirectAppId);
                }
                if (lookup.ManifestId == 0)
                {
                    continue;
                }
                var key = await gateway.GetDepotKeyAsync(depotId, appId, ct);
                if (key is null)
                {
                    noAccess++;
                    continue;
                }
                var name = $"{depotId}_{lookup.ManifestId}.manifest";
                var appDir = Path.Combine(outDir, appId.ToString());
                Directory.CreateDirectory(appDir);
                var containing = DepotSelector.ContainingAppId(info, depotId, appId);
                await gateway.SaveManifestAsync(depotId, containing, lookup.ManifestId, key, Path.Combine(appDir, name), ct);
                saved.Add(name);
            }

            if (saved.Count > 0)
            {
                return new AppResult(appId, AppStatus.Ok, Manifests: saved);
            }
            return noAccess > 0
                ? new AppResult(appId, AppStatus.NotOwned, Reason: $"no access to {noAccess} depot(s)")
                : new AppResult(appId, AppStatus.NoDepots, Reason: "no public manifest for any selected depot");
        }
        catch (SteamRequestException e)
        {
            return new AppResult(appId, AppStatus.Error, Reason: e.Message);
        }
    }
}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
```

Expected: `Passed!  - Failed: 0, Passed: 30`.

- [ ] **Step 6: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 4 of the approved 361 plan: one-login fetch runner with a single reconnect, test-first"
```

```bash
git add tools/steam_manifest_helper/SteamManifestHelper/ISteamGateway.cs tools/steam_manifest_helper/SteamManifestHelper/Results.cs tools/steam_manifest_helper/SteamManifestHelper/FetchRunner.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/FakeSteamGateway.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/FetchRunnerTests.cs
git status --short
git commit -m "feat(steam): helper fetch runner, one login per run, one reconnect (#361)"
```

---

### Task 5: The real Steam connection, the CDN 403 rule, and the command line

**Files:**
- Create: `tools/steam_manifest_helper/SteamManifestHelper/CdnAuth.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/SteamKitGateway.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/LoginCommand.cs`
- Create: `tools/steam_manifest_helper/SteamManifestHelper/Cli.cs`
- Modify: `tools/steam_manifest_helper/SteamManifestHelper/Program.cs` (replace the placeholder)
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/CdnAuthTests.cs`
- Test: `tools/steam_manifest_helper/SteamManifestHelper.Tests/CliTests.cs`

**Interfaces:**
- Consumes: everything from Tasks 2-4.
- Produces:
  - `static class CdnAuth` with:
    - `Task<T> WithAuthRetryAsync<T>(Func<string?, Task<T>> download, string? knownToken, Func<Task<string?>> fetchToken)`
    - `bool IsForbidden(Exception)`
  - `class SteamKitGateway(TextWriter log) : ISteamGateway`
  - `static class LoginCommand` with `Task<int> RunAsync(string username, string sessionDir, TextWriter log, CancellationToken ct)`
  - `static class Cli` with:
    - `Task<int> RunAsync(string[] args, TextWriter stdout, TextWriter stderr, Func<ISteamGateway> gatewayFactory, CancellationToken ct)`
    - internal `Dictionary<string,string>? ParseOptions(string[] args, int start)`
    - internal `IReadOnlyList<uint> ReadAppIds(string path)`
  - The command line Python calls:
    - `SteamManifestHelper fetch --apps <file> --out <dir> --session-dir <dir> --username <name> [--import-from <dir>]`
    - `SteamManifestHelper login --username <name> --session-dir <dir>`

Before writing `SteamKitGateway.cs`, apply every compile fix recorded in Task 1's `REPORT.txt`. The spike needed none. The gateway below mirrors the spike's calls.

**Rulings from the Task 1 gate (2026-10-07). Apply these to the gateway code below:**
1. **Fixed login ID.**
   - What: set `LoginID = 0x534D48, // "SMH"` in the `LogOnDetails`.
   - Why: DepotDownloader 3.4.0 sets its own fixed ID (`LoginID = Config.LoginID ?? 0x534B32`, `ContentDownloader.cs:315`). A fixed ID of the helper's own keeps it from colliding with another SteamKit client that derives its ID from the same host.
2. **A Steam log-off counts as a lost session.**
   - What: subscribe to `SteamUser.LoggedOffCallback`. Handle it like `OnDisconnected`: set `online = false` and fail the pending signals with `SessionLostException($"Steam logged the session off: {cb.Result}")`.
   - Why: without it, a log-off such as `LoggedInElsewhere` leaves `online` true. Every later request then times out after 60 s as a per-app error, until Python's 2 h kill throws away the whole run. DepotDownloader 3.4.0 has no such handler; this is the spec's session-loss row.
3. **Try up to 6 CDN servers, not 3.**
   - What: in `SaveManifestAsync`, change `.Take(3)` to `.Take(6)`.
     - A `401`, a `404`, or a `403` that survives `CdnAuth`'s one token retry stops the server loop at once. These mirror DepotDownloader's "Aborting" cases (`ContentDownloader.cs:825-835`).
     - Any other failure (503, timeout) moves on to the next server.
   - Why: in the spike, depot 17343 failed with 503 from all 3 servers it tried. DepotDownloader rotates servers until one works, with no cap. 6 keeps the loop bounded.
   - Test: none new. `SteamKitGateway` is not unit-tested by design; it is proven live in Task 10. The task reviewer checks the stop rule by reading the code.

- [ ] **Step 1: Write the failing tests**

`CdnAuthTests.cs`:

```csharp
using System.Net;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class CdnAuthTests
{
    private static HttpRequestException Forbidden() => new("forbidden", null, HttpStatusCode.Forbidden);

    [Fact]
    public async Task A_403_fetches_a_cdn_token_once_and_retries_with_it()
    {
        // Review Focus 3.
        var seen = new List<string?>();
        var fetches = 0;
        var result = await CdnAuth.WithAuthRetryAsync(
            token =>
            {
                seen.Add(token);
                return token is null ? Task.FromException<string>(Forbidden()) : Task.FromResult("manifest");
            },
            knownToken: null,
            fetchToken: () => { fetches++; return Task.FromResult<string?>("cdn-token"); });
        Assert.Equal("manifest", result);
        Assert.Equal(new string?[] { null, "cdn-token" }, seen);
        Assert.Equal(1, fetches);
    }

    [Fact]
    public async Task A_403_with_no_token_available_rethrows()
    {
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(Forbidden()),
            knownToken: null,
            fetchToken: () => Task.FromResult<string?>(null)));
    }

    [Fact]
    public async Task A_403_that_already_used_a_token_does_not_fetch_another()
    {
        var fetches = 0;
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(Forbidden()),
            knownToken: "cdn-token",
            fetchToken: () => { fetches++; return Task.FromResult<string?>("another"); }));
        Assert.Equal(0, fetches);
    }

    [Fact]
    public async Task Other_failures_are_not_retried()
    {
        var fetches = 0;
        await Assert.ThrowsAsync<HttpRequestException>(() => CdnAuth.WithAuthRetryAsync(
            _ => Task.FromException<string>(new HttpRequestException("gone", null, HttpStatusCode.NotFound)),
            knownToken: null,
            fetchToken: () => { fetches++; return Task.FromResult<string?>("cdn-token"); }));
        Assert.Equal(0, fetches);
    }
}
```

`CliTests.cs`:

```csharp
using System.Text.Json;
using SteamManifestHelper;
using Xunit;

namespace SteamManifestHelper.Tests;

public sealed class CliTests : IDisposable
{
    private readonly string root = Directory.CreateTempSubdirectory("smh-cli-").FullName;

    public void Dispose() => Directory.Delete(root, recursive: true);

    [Fact]
    public async Task No_arguments_is_a_usage_error()
    {
        var stderr = new StringWriter();
        var code = await Cli.RunAsync([], new StringWriter(), stderr, () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.Usage, code);
        Assert.Contains("usage", stderr.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task A_missing_required_option_is_a_usage_error()
    {
        var code = await Cli.RunAsync(["fetch", "--apps", "x"], new StringWriter(), new StringWriter(), () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.Usage, code);
    }

    [Fact]
    public async Task Fetch_without_any_session_exits_3_and_names_the_login_command()
    {
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var stdout = new StringWriter();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", Path.Combine(root, "s"), "--username", "kraulerson", "--import-from", Path.Combine(root, "nope")],
            stdout, new StringWriter(), () => new FakeSteamGateway(), CancellationToken.None);
        Assert.Equal(ExitCodes.ImportFailed, code);
        var summary = JsonDocument.Parse(stdout.ToString().Trim()).RootElement;
        Assert.Equal("import_failed", summary.GetProperty("session").GetString());
        Assert.Contains("login --username kraulerson", summary.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task Fetch_with_a_saved_session_runs_the_fetch()
    {
        var sessionDir = Path.Combine(root, "s");
        SessionStore.Save(sessionDir, new SteamSession("kraulerson", "eyJ-test"));
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n");
        var gateway = new FakeSteamGateway();
        var code = await Cli.RunAsync(
            ["fetch", "--apps", apps, "--out", Path.Combine(root, "out"), "--session-dir", sessionDir, "--username", "kraulerson"],
            new StringWriter(), new StringWriter(), () => gateway, CancellationToken.None);
        Assert.Equal(ExitCodes.Completed, code);
        Assert.Equal(1, gateway.LogOnCalls);
    }

    [Fact]
    public void App_ids_are_read_once_each_and_blank_lines_are_skipped()
    {
        var apps = Path.Combine(root, "apps.txt");
        File.WriteAllText(apps, "10\n\n20\n10\n 30 \n");
        Assert.Equal(new uint[] { 10, 20, 30 }, Cli.ReadAppIds(apps));
    }
}
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
```

Expected: build errors, `'CdnAuth' could not be found` and `'Cli' could not be found`.

- [ ] **Step 3: Implement `CdnAuth.cs`**

```csharp
using System.Net;
using SteamKit2;

namespace SteamManifestHelper;

/// <summary>DepotDownloader's CDN rule (ContentDownloader.cs:815-824): a 403 with no
/// CDN auth token yet means "get one and try again", once.</summary>
public static class CdnAuth
{
    public static async Task<T> WithAuthRetryAsync<T>(Func<string?, Task<T>> download, string? knownToken, Func<Task<string?>> fetchToken)
    {
        try
        {
            return await download(knownToken);
        }
        catch (Exception e) when (knownToken is null && IsForbidden(e))
        {
            var token = await fetchToken();
            if (token is null)
            {
                throw;
            }
            return await download(token);
        }
    }

    public static bool IsForbidden(Exception e) => e switch
    {
        SteamKitWebRequestException web => web.StatusCode == HttpStatusCode.Forbidden,
        HttpRequestException http => http.StatusCode == HttpStatusCode.Forbidden,
        _ => false,
    };
}
```

- [ ] **Step 4: Implement `SteamKitGateway.cs`**

```csharp
using SteamKit2;
using SteamKit2.CDN;

namespace SteamManifestHelper;

/// <summary>ISteamGateway on SteamKit2 3.4.0, mirroring DepotDownloader 3.4.0's calls
/// (Steam3Session.cs, CDNClientPool.cs, ContentDownloader.cs:740-860). Not
/// unit-tested: proven live by the Task 1 spike and the Task 10 first run.</summary>
public sealed class SteamKitGateway : ISteamGateway
{
    private static readonly TimeSpan StepTimeout = TimeSpan.FromSeconds(60);
    private readonly TextWriter log;
    private readonly SteamClient client = new();
    private readonly CallbackManager callbacks;
    private readonly SteamUser user;
    private readonly SteamApps apps;
    private readonly SteamContent content;
    private readonly Client cdn;
    private readonly CancellationTokenSource pumpStop = new();
    private readonly Task pump;
    private readonly Dictionary<uint, KeyValue?> appInfo = new();
    private readonly Dictionary<(uint Depot, string Host), string> cdnTokens = new();
    private TaskCompletionSource connected = NewSignal();
    private TaskCompletionSource<EResult> loggedOn = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private volatile bool online;
    private List<Server>? servers;

    public SteamKitGateway(TextWriter log)
    {
        this.log = log;
        callbacks = new CallbackManager(client);
        user = client.GetHandler<SteamUser>()!;
        apps = client.GetHandler<SteamApps>()!;
        content = client.GetHandler<SteamContent>()!;
        cdn = new Client(client);
        callbacks.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
        callbacks.Subscribe<SteamClient.DisconnectedCallback>(OnDisconnected);
        callbacks.Subscribe<SteamUser.LoggedOnCallback>(cb => loggedOn.TrySetResult(cb.Result));
        pump = Task.Run(() =>
        {
            while (!pumpStop.IsCancellationRequested)
            {
                callbacks.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
            }
        });
    }

    private static TaskCompletionSource NewSignal() => new(TaskCreationOptions.RunContinuationsAsynchronously);

    private void OnDisconnected(SteamClient.DisconnectedCallback cb)
    {
        online = false;
        var lost = new SessionLostException(cb.UserInitiated ? "disconnected by the helper" : "Steam closed the connection");
        connected.TrySetException(lost);
        loggedOn.TrySetException(lost);
    }

    public async Task<LogOnResult> ConnectAndLogOnAsync(SteamSession session, CancellationToken ct)
    {
        connected = NewSignal();
        loggedOn = new TaskCompletionSource<EResult>(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            client.Connect();
            await connected.Task.WaitAsync(StepTimeout, ct);
            user.LogOn(new SteamUser.LogOnDetails
            {
                Username = session.Username,
                AccessToken = session.RefreshToken,
                ShouldRememberPassword = true,
            });
            var result = await loggedOn.Task.WaitAsync(StepTimeout, ct);
            if (result == EResult.OK)
            {
                online = true;
                log.WriteLine($"logged on as {session.Username}");
                return new LogOnResult(LogOnOutcome.Ok, "OK");
            }
            client.Disconnect();
            // The results DepotDownloader treats as "this token is dead" (Steam3Session.cs:594-599).
            return result is EResult.InvalidPassword or EResult.InvalidSignature or EResult.AccessDenied
                or EResult.Expired or EResult.Revoked
                ? new LogOnResult(LogOnOutcome.TokenRejected, result.ToString())
                : new LogOnResult(LogOnOutcome.Refused, result.ToString());
        }
        catch (Exception e) when (e is SessionLostException or TimeoutException)
        {
            return new LogOnResult(LogOnOutcome.Refused, e.Message);
        }
    }

    private void EnsureOnline()
    {
        if (!online)
        {
            throw new SessionLostException("not connected to Steam");
        }
    }

    /// <summary>A failure while still online is one bad request; a failure after the
    /// connection dropped is a lost session.</summary>
    private async Task<T> Call<T>(Func<Task<T>> call, string what, CancellationToken ct)
    {
        EnsureOnline();
        try
        {
            return await call().WaitAsync(StepTimeout, ct);
        }
        catch (Exception e) when (!ct.IsCancellationRequested && e is not SessionLostException)
        {
            EnsureOnline();
            throw new SteamRequestException($"{what}: {e.GetType().Name}: {e.Message}");
        }
    }

    public async Task<KeyValue?> GetAppInfoAsync(uint appId, CancellationToken ct)
    {
        if (appInfo.TryGetValue(appId, out var cached))
        {
            return cached;
        }
        var tokens = await Call(() => apps.PICSGetAccessTokens([appId], []).ToTask(), $"access token for app {appId}", ct);
        var request = new SteamApps.PICSRequest(appId);
        if (tokens.AppTokens.TryGetValue(appId, out var token))
        {
            request.AccessToken = token;
        }
        var info = await Call(() => apps.PICSGetProductInfo([request], []).ToTask(), $"app info for {appId}", ct);
        KeyValue? keyValues = null;
        foreach (var part in info.Results ?? [])
        {
            if (part.Apps.TryGetValue(appId, out var app))
            {
                keyValues = app.KeyValues;
            }
        }
        appInfo[appId] = keyValues;
        return keyValues;
    }

    public async Task<byte[]?> GetDepotKeyAsync(uint depotId, uint appId, CancellationToken ct)
    {
        var key = await Call(() => apps.GetDepotDecryptionKey(depotId, appId).ToTask(), $"depot key {depotId}", ct);
        return key.Result == EResult.OK ? key.DepotKey : null;
    }

    public async Task SaveManifestAsync(uint depotId, uint containingAppId, ulong manifestId, byte[] depotKey, string path, CancellationToken ct)
    {
        servers ??= (await Call(() => content.GetServersForSteamPipe(), "CDN server list", ct))
            .Where(s => s.Type is "SteamCache" or "CDN")
            .OrderBy(s => s.WeightedLoad)
            .ToList();
        var code = await Call(() => content.GetManifestRequestCode(depotId, containingAppId, manifestId, "public"),
            $"request code for depot {depotId}", ct);
        if (code == 0)
        {
            throw new SteamRequestException($"no manifest request code for depot {depotId}");
        }

        Exception? last = null;
        foreach (var server in servers.Where(s => s.AllowedAppIds.Length == 0 || s.AllowedAppIds.Contains(containingAppId)).Take(3))
        {
            var host = server.Host!;
            cdnTokens.TryGetValue((depotId, host), out var known);
            try
            {
                var manifest = await CdnAuth.WithAuthRetryAsync(
                    token => cdn.DownloadManifestAsync(depotId, manifestId, code, server, depotKey, null, token),
                    known,
                    async () =>
                    {
                        var auth = await Call(() => content.GetCDNAuthToken(containingAppId, depotId, host), $"CDN token for {host}", ct);
                        if (auth.Result != EResult.OK || auth.Token is null)
                        {
                            return null;
                        }
                        cdnTokens[(depotId, host)] = auth.Token;
                        return auth.Token;
                    });
                manifest.SaveToFile(path);
                return;
            }
            catch (Exception e) when (!ct.IsCancellationRequested && e is not SessionLostException)
            {
                EnsureOnline();
                last = e;
            }
        }
        throw new SteamRequestException($"manifest {manifestId} for depot {depotId}: {last?.GetType().Name}: {last?.Message}");
    }

    public async ValueTask DisposeAsync()
    {
        if (online)
        {
            user.LogOff();
        }
        client.Disconnect();
        pumpStop.Cancel();
        await pump;
    }
}
```

- [ ] **Step 5: Implement `LoginCommand.cs`**

```csharp
using System.Text;
using SteamKit2;
using SteamKit2.Authentication;

namespace SteamManifestHelper;

/// <summary>One-time interactive login: password, then Steam Guard approval on the
/// phone. Needed when the imported token is rejected or expires, because nothing
/// else can renew the session once DepotDownloader is gone.</summary>
public static class LoginCommand
{
    public static async Task<int> RunAsync(string username, string sessionDir, TextWriter log, CancellationToken ct)
    {
        log.Write($"Steam password for {username}: ");
        var password = ReadSecret();
        var client = new SteamClient();
        var manager = new CallbackManager(client);
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        manager.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
        manager.Subscribe<SteamClient.DisconnectedCallback>(_ => connected.TrySetException(new SessionLostException("disconnected before login")));
        using var pumpStop = new CancellationTokenSource();
        var pump = Task.Run(() =>
        {
            while (!pumpStop.IsCancellationRequested)
            {
                manager.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
            }
        });
        try
        {
            client.Connect();
            await connected.Task.WaitAsync(TimeSpan.FromSeconds(30), ct);
            var auth = await client.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails
            {
                Username = username,
                Password = password,
                IsPersistentSession = true,
                Authenticator = new UserConsoleAuthenticator(),
            });
            var result = await auth.PollingWaitForResultAsync(ct);
            SessionStore.Save(sessionDir, new SteamSession(result.AccountName, result.RefreshToken));
            log.WriteLine($"Saved a login for {result.AccountName} to {Path.Combine(sessionDir, SessionStore.FileName)}");
            return ExitCodes.Completed;
        }
        catch (AuthenticationException e)
        {
            log.WriteLine($"Steam refused the login: {e.Result}");
            return ExitCodes.LoginRefused;
        }
        finally
        {
            client.Disconnect();
            pumpStop.Cancel();
            await pump;
        }
    }

    private static string ReadSecret()
    {
        var secret = new StringBuilder();
        ConsoleKeyInfo key;
        while ((key = Console.ReadKey(intercept: true)).Key != ConsoleKey.Enter)
        {
            if (key.Key == ConsoleKey.Backspace)
            {
                if (secret.Length > 0)
                {
                    secret.Length--;
                }
            }
            else
            {
                secret.Append(key.KeyChar);
            }
        }
        Console.Error.WriteLine();
        return secret.ToString();
    }
}
```

- [ ] **Step 6: Implement `Cli.cs` and replace `Program.cs`**

`Cli.cs`:

```csharp
namespace SteamManifestHelper;

public static class Cli
{
    private const string Usage =
        "usage: SteamManifestHelper fetch --apps <file> --out <dir> --session-dir <dir> --username <name> [--import-from <dir>]\n" +
        "       SteamManifestHelper login --username <name> --session-dir <dir>";

    public static async Task<int> RunAsync(string[] args, TextWriter stdout, TextWriter stderr, Func<ISteamGateway> gatewayFactory, CancellationToken ct)
    {
        var command = args.Length > 0 ? args[0] : "";
        var options = args.Length > 0 ? ParseOptions(args, 1) : null;
        string[] required = command == "fetch"
            ? ["--apps", "--out", "--session-dir", "--username"]
            : ["--username", "--session-dir"];
        if (command is not ("fetch" or "login") || options is null || required.Any(r => !options.ContainsKey(r)))
        {
            stderr.WriteLine(Usage);
            return ExitCodes.Usage;
        }
        try
        {
            return command == "fetch"
                ? await FetchAsync(options, stdout, stderr, gatewayFactory, ct)
                : await LoginCommand.RunAsync(options["--username"], options["--session-dir"], stderr, ct);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            stderr.WriteLine($"unexpected: {e.GetType().Name}: {e.Message}");
            return ExitCodes.Unexpected;
        }
    }

    private static async Task<int> FetchAsync(Dictionary<string, string> options, TextWriter stdout, TextWriter stderr, Func<ISteamGateway> gatewayFactory, CancellationToken ct)
    {
        var username = options["--username"];
        var sessionDir = options["--session-dir"];
        SteamSession session;
        try
        {
            session = SessionStore.Load(sessionDir, username, options.GetValueOrDefault("--import-from"));
        }
        catch (SessionImportException e)
        {
            var reason = $"{e.Message}. Run: SteamManifestHelper login --username {username} --session-dir {sessionDir}";
            stderr.WriteLine(reason);
            ResultWriter.Write(stdout, new RunSummary(SessionStatus.ImportFailed, reason));
            return ExitCodes.ImportFailed;
        }
        var appIds = ReadAppIds(options["--apps"]);
        await using var gateway = gatewayFactory();
        var runner = new FetchRunner(gateway, stdout, stderr, (wait, token) => Task.Delay(wait, token));
        return await runner.RunAsync(session, appIds, options["--out"], ct);
    }

    internal static Dictionary<string, string>? ParseOptions(string[] args, int start)
    {
        var options = new Dictionary<string, string>();
        for (var i = start; i < args.Length; i += 2)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal) || i + 1 >= args.Length)
            {
                return null;
            }
            options[args[i]] = args[i + 1];
        }
        return options;
    }

    internal static IReadOnlyList<uint> ReadAppIds(string path) =>
        File.ReadLines(path).Select(l => l.Trim()).Where(l => l.Length > 0).Select(uint.Parse).Distinct().ToList();
}
```

`Program.cs` (replace the whole file):

```csharp
using SteamManifestHelper;

return await Cli.RunAsync(args, Console.Out, Console.Error, () => new SteamKitGateway(Console.Error), CancellationToken.None);
```

- [ ] **Step 7: Run all helper tests to verify they pass, and smoke-test the usage path**

```bash
cd tools/steam_manifest_helper && dotnet test SteamManifestHelper.Tests 2>&1 | tail -3
dotnet run --project SteamManifestHelper -- ; echo "exit=$?"
```

Expected: `Passed!  - Failed: 0, Passed: 39`, then the usage text and `exit=64`.

- [ ] **Step 8: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 5 of the approved 361 plan: real SteamKit2 gateway, CDN 403 rule, login command and CLI"
```

```bash
git add tools/steam_manifest_helper/SteamManifestHelper/CdnAuth.cs tools/steam_manifest_helper/SteamManifestHelper/SteamKitGateway.cs tools/steam_manifest_helper/SteamManifestHelper/LoginCommand.cs tools/steam_manifest_helper/SteamManifestHelper/Cli.cs tools/steam_manifest_helper/SteamManifestHelper/Program.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/CdnAuthTests.cs tools/steam_manifest_helper/SteamManifestHelper.Tests/CliTests.cs
git status --short
git commit -m "feat(steam): helper Steam gateway, CDN auth retry, login command, CLI (#361)"
```

---

### Task 6: Build it into the image and drop DepotDownloader

**Files:**
- Modify: `Dockerfile`: remove the DepotDownloader `ARG`/`RUN` block (lines 23-35 of the builder stage), add a `helper` stage, and swap the runtime `COPY`.
- Modify: `.github/workflows/ci.yml`, the "Verify image size (amd64)" step (~lines 207-213). Change the limit from `250` to `275` in both the comparison and the error message, and nowhere else. Karl's ruling, 2026-10-07 (Global Constraints, refinement 4).
- Create: `tools/steam_manifest_helper/licenses/SteamKit2-LGPL-2.1.txt`
- Create: `tools/steam_manifest_helper/licenses/protobuf-net-Apache-2.0.txt`
- Create: `tools/steam_manifest_helper/licenses/ZstdSharp.Port-MIT.txt`
- Create: `tools/steam_manifest_helper/licenses/System.IO.Hashing-MIT.txt`
- Create: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Produces: `/steam-manifest-helper/SteamManifestHelper` inside the image. Task 8's setting default points here.

- [ ] **Step 1: Fetch the license texts**

```bash
L=tools/steam_manifest_helper/licenses && mkdir -p "$L"
curl -fsSL https://raw.githubusercontent.com/SteamRE/SteamKit/3.4.0/LICENSE -o "$L/SteamKit2-LGPL-2.1.txt"
curl -fsSL https://raw.githubusercontent.com/protobuf-net/protobuf-net/main/Licence.txt -o "$L/protobuf-net-Apache-2.0.txt"
curl -fsSL https://raw.githubusercontent.com/oleg-st/ZstdSharp/master/LICENSE -o "$L/ZstdSharp.Port-MIT.txt"
curl -fsSL https://raw.githubusercontent.com/dotnet/runtime/main/LICENSE.TXT -o "$L/System.IO.Hashing-MIT.txt"
head -2 "$L"/*.txt
```

Expected first lines, all four URLs confirmed live on 2026-10-07:
- `GNU LESSER GENERAL PUBLIC LICENSE`
- `The core Protocol Buffers technology is provided courtesy of`
- `MIT License`
- `The MIT License (MIT)`

- [ ] **Step 2: Edit the Dockerfile**

Delete the builder-stage block that starts `# DepotDownloader — pinned binary` and ends `rm /tmp/dd.zip`. Then insert this stage directly above `# ── Stage 2: runtime`:

```dockerfile
# ── Stage 1b: Steam manifest helper (.NET, #361) ─────────────────
# Built on the BUILD platform and cross-published for the TARGET architecture, so
# CI's arm64 build never runs the .NET compiler under QEMU. The tests run first:
# a failing helper test fails the image build. Published self-contained but NOT
# single-file and NOT trimmed, so SteamKit2.dll (LGPL-2.1) stays a separate,
# replaceable file.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317 AS helper
ARG TARGETARCH
WORKDIR /src
COPY tools/steam_manifest_helper/ ./
RUN dotnet restore SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj --locked-mode \
 && dotnet test SteamManifestHelper.Tests/SteamManifestHelper.Tests.csproj --no-restore -c Release
RUN RID="linux-$( [ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64 )" \
 && dotnet restore SteamManifestHelper/SteamManifestHelper.csproj --locked-mode -r "$RID" \
 && dotnet publish SteamManifestHelper/SteamManifestHelper.csproj --no-restore -c Release -r "$RID" \
      --self-contained true -p:PublishSingleFile=false -p:PublishTrimmed=false -o /steam-manifest-helper \
 && cp -r licenses /steam-manifest-helper/licenses
```

In the runtime stage, replace

```dockerfile
# linux-x64 build is self-contained (bundles .NET); the image is shared
# control+agent but only the agent invokes it.
COPY --from=builder /depotdownloader /depotdownloader
```

with

```dockerfile
# Self-contained .NET helper (#361); the image is shared control+agent, but only
# the agent invokes it.
COPY --from=helper /steam-manifest-helper /steam-manifest-helper
```

- [ ] **Step 3: Write `THIRD_PARTY_NOTICES.md`**

```markdown
# Third-party notices

The orchestrator's own code is MIT. These components ship inside its container
image, unmodified, each under its own license. Their license texts are in the
image at `/steam-manifest-helper/licenses/`.

| Component | Version | License | Where | How it is used |
|---|---|---|---|---|
| SteamKit2 | 3.4.0 | LGPL-2.1-only | `/steam-manifest-helper/SteamKit2.dll` | Library called by the Steam manifest helper. Shipped as a separate, replaceable assembly, not merged or trimmed, as the LGPL requires |
| protobuf-net | 3.2.56 | Apache-2.0 | `/steam-manifest-helper/` | Dependency of SteamKit2; also reads DepotDownloader's saved login once |
| ZstdSharp.Port | 0.8.7 | MIT | `/steam-manifest-helper/` | Dependency of SteamKit2 |
| System.IO.Hashing | 10.0.1 | MIT | `/steam-manifest-helper/` | Dependency of SteamKit2 |
| .NET runtime | 10.0 | MIT | `/steam-manifest-helper/` | Bundled by the self-contained publish |

Python dependencies are checked against an allow-list by `tests/test_licenses.py`.

DepotDownloader (GPL-2.0) shipped in the image as a separate program until #361
replaced it. None of its code is included in the helper.
```

- [ ] **Step 4: Build the image on the LXC and check its size and contents**

The Mac's Docker has no `buildx` plugin. Without BuildKit, `$BUILDPLATFORM` and `$TARGETARCH` are empty and the `helper` stage cannot build. The LXC's Docker 29 uses BuildKit. Push the branch, then build in a throwaway worktree so the deploy clone at `/root/lancache-orchestrator` stays on `main`:

```bash
git push -u origin HEAD
B=$(git branch --show-current)
ssh root@10.100.23.105 "cd /root/lancache-orchestrator && git fetch -q origin && git worktree add -f /root/helper-check origin/$B && cd /root/helper-check && docker build -t orchestrator:helper-check . 2>&1 | tail -5"
ssh root@10.100.23.105 "docker image inspect orchestrator:helper-check --format '{{.Size}}' | awk '{printf \"%d MiB\n\", \$1/1048576}'"
ssh root@10.100.23.105 "docker run --rm --entrypoint sh orchestrator:helper-check -c 'ls /steam-manifest-helper/SteamKit2.dll /steam-manifest-helper/licenses; /steam-manifest-helper/SteamManifestHelper; echo exit=\$?; ls /depotdownloader 2>&1 | head -1'"
ssh root@10.100.23.105 "cd /root/lancache-orchestrator && git worktree remove --force /root/helper-check && docker rmi orchestrator:helper-check >/dev/null"
```

Expected:
- the build reports the helper tests `Passed!` and finishes;
- the size is **at most 275 MiB** (expect about 254);
- `SteamKit2.dll` and `licenses` are present;
- the usage text and `exit=64`;
- `No such file or directory` for `/depotdownloader`.

**Over 275 MiB: STOP and report to Karl.** Do not trim.

- [ ] **Step 5: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 6 of the approved 361 plan: build the helper into the image and remove DepotDownloader"
```

```bash
git add Dockerfile .github/workflows/ci.yml THIRD_PARTY_NOTICES.md tools/steam_manifest_helper/licenses/SteamKit2-LGPL-2.1.txt tools/steam_manifest_helper/licenses/protobuf-net-Apache-2.0.txt tools/steam_manifest_helper/licenses/ZstdSharp.Port-MIT.txt tools/steam_manifest_helper/licenses/System.IO.Hashing-MIT.txt
git status --short
git commit -m "build(docker): ship the Steam manifest helper, drop DepotDownloader (#361)"
```

---

### Task 7: The Python fetcher calls the helper once

**Files:**
- Modify: `src/orchestrator/platform/steam/manifest_fetcher.py`. Rewrite the class; keep `_enumerate_app_ids`, `_app_ids_with_ext`, `_write_shas`, `FetchResult`, `SteamAuthError`, `_SHA1_RE` and `_SESSION_GLOB` unchanged.
- Test: `tests/platform/steam/test_manifest_fetcher.py`. Replace the DepotDownloader-specific tests; keep every `_enumerate*`, `write_shas` and `skipped_all_archived` test, updated to the new constructor.

**Interfaces:**
- Consumes: the helper CLI and JSON lines (Tasks 4-5), and `parse_steamkit_manifest(bytes) -> set[str]` (existing).
- Produces:
  - `class SteamManifestFetcher(*, binary: Path, config_dir: Path, steam_config_dir: Path, archive_dir: Path, username: str = "", timeout_sec: float = 7200.0, manifest_cache_dir: Path | None = None)` with `fetch_all() -> FetchResult` and `login_from_session() -> None`.
  - `class HelperSessionError(RuntimeError)`.
- Removed: `DepotDownloaderManifestFetcher`, `TransientFetchError`, `_TRANSIENT_RE`, `_MAX_BACKOFF_SEC`.

- [ ] **Step 1: Write the failing tests**

Replace the imports and helpers at the top of `tests/platform/steam/test_manifest_fetcher.py` (lines 1-58) with:

```python
import json
import os
import struct
import sys

import pytest
from structlog.testing import capture_logs

from orchestrator.platform.steam.manifest_fetcher import (
    FetchResult,
    HelperSessionError,
    SteamAuthError,
    SteamManifestFetcher,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _make_session(config_dir):
    """Create the .NET IsolatedStorage account.config DepotDownloader persists
    under HOME (=config_dir), which the helper imports from."""
    p = config_dir / ".local/share/IsolatedStorage/aa/bb/cc/AssemFiles/account.config"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00token")
    return p


def _manifest_bytes(shas):
    """A minimal SteamKit2 manifest payload holding these chunk SHAs, built the
    same way as tests/platform/steam/test_steamkit_manifest_parser.py."""

    def ld(field, payload):
        return bytes([(field << 3) | 2, len(payload)]) + payload

    filemap = b"".join(ld(6, ld(1, bytes.fromhex(s))) for s in shas)
    payload = ld(1, filemap)
    return struct.pack("<II", 0x71F617D0, len(payload)) + payload


def _fetcher(tmp_path, **kw):
    return SteamManifestFetcher(
        binary=kw.get("binary", tmp_path / "SteamManifestHelper"),
        config_dir=kw.get("config_dir", tmp_path / "dd-config"),
        steam_config_dir=kw.get("steam_config_dir", tmp_path / "Config"),
        archive_dir=kw.get("archive_dir", tmp_path / "archive"),
        username="kraulerson",
        timeout_sec=kw.get("timeout_sec", 30.0),
        manifest_cache_dir=kw.get("manifest_cache_dir"),
    )


def _fake_helper(tmp_path, *, lines, exit_code=0, manifests=None, extra_stdout="", sleep=0.0):
    """Write an executable stand-in for SteamManifestHelper. It records its argv and
    the app list it was given, writes `manifests` ({app: {name: [shas]}}) under
    --out, prints `lines` as JSON (plus `extra_stdout`), and exits `exit_code`."""
    spec = {
        "lines": lines,
        "exit": exit_code,
        "manifests": {str(app): {n: _manifest_bytes(s).hex() for n, s in files.items()} for app, files in (manifests or {}).items()},
        "extra": extra_stdout,
        "sleep": sleep,
        "record": str(tmp_path),
    }
    script = tmp_path / "SteamManifestHelper"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        f"spec = json.loads({json.dumps(json.dumps(spec))})\n"
        "rec = pathlib.Path(spec['record'])\n"
        "with open(rec / 'calls.log', 'a') as f: f.write('call\\n')\n"
        "(rec / 'argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "opts = dict(zip(sys.argv[2::2], sys.argv[3::2]))\n"
        "(rec / 'apps-seen.txt').write_text(pathlib.Path(opts['--apps']).read_text())\n"
        "for app, files in spec['manifests'].items():\n"
        "    d = pathlib.Path(opts['--out']) / app\n"
        "    d.mkdir(parents=True, exist_ok=True)\n"
        "    for name, hexbytes in files.items(): (d / name).write_bytes(bytes.fromhex(hexbytes))\n"
        "time.sleep(spec['sleep'])\n"
        "if spec['extra']: print(spec['extra'])\n"
        "for line in spec['lines']: print(json.dumps(line))\n"
        "sys.stderr.write('helper log line\\n')\n"
        "sys.exit(spec['exit'])\n"
    )
    script.chmod(0o755)
    return script


def _setup(tmp_path, apps):
    cfg = tmp_path / "dd-config"
    cfg.mkdir()
    _make_session(cfg)
    steam_cfg = tmp_path / "Config"
    steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text(json.dumps(apps))


_DONE = {"summary": True, "session": "completed", "reason": ""}
```

Delete these tests, which pin DepotDownloader behaviour that no longer exists:
- `test_fetch_all_writes_shas_per_depot`
- `test_fetch_all_idempotent_skip_existing`
- `test_fetch_all_isolates_per_app_failure`
- `test_run_manifest_only_includes_username_in_argv`
- `test_run_manifest_only_pins_home_to_config_dir`
- `test_fetch_all_raises_on_total_failure`
- `_run_returns`
- `test_run_manifest_only_classifies_transient_dd_failure`
- `test_run_manifest_only_permanent_failure_not_transient`
- `test_fetch_all_retries_transient_then_succeeds`
- `test_fetch_all_bounds_transient_retries_then_counts_failed`

Keep:
- `test_login_from_session_*`
- `test_fetch_result_fields`
- `test_fetch_all_raises_auth_when_no_session`
- `test_write_shas_empty_returns_false_no_file`
- `test_fetch_all_skipped_all_archived_no_raise`
- `test_enumerate_*`

Change their `DepotDownloaderManifestFetcher(...)` calls to `SteamManifestFetcher(...)`, dropping `delay_sec=` and adding `username="kraulerson"`. In `test_fetch_all_skipped_all_archived_no_raise`, replace the `_run_manifest_only` monkeypatch with `_fake_helper(tmp_path, lines=[{"app": 10, "status": "ok", "manifests": ["100_1.manifest"]}, _DONE], manifests={10: {"100_1.manifest": [_SHA_A]}})`, after pre-writing `archive/v1/10_10_100_1.shas`.

Delete `test_username_default_is_empty` and `test_username_stored_when_provided`. The username is now a required argument of the helper, pinned by `test_one_helper_call_carries_every_selected_app` below.

Then append:

```python
def test_one_helper_call_carries_every_selected_app(tmp_path):
    _setup(tmp_path, [10, 20, 30])
    _fake_helper(tmp_path, lines=[_DONE])
    _fetcher(tmp_path).fetch_all()
    assert (tmp_path / "calls.log").read_text().count("call") == 1
    assert (tmp_path / "apps-seen.txt").read_text().split() == ["10", "20", "30"]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0] == "fetch"
    opts = dict(zip(argv[1::2], argv[2::2]))
    assert opts["--username"] == "kraulerson"
    assert opts["--session-dir"] == str(tmp_path / "dd-config" / "steam-manifest-helper")
    assert opts["--import-from"] == str(tmp_path / "dd-config")


def test_ok_manifests_become_shas_sidecars(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_555.manifest"]}, _DONE],
        manifests={10: {"100_555.manifest": [_SHA_B, _SHA_A]}},
    )
    result = _fetcher(tmp_path).fetch_all()
    assert result == FetchResult(fetched=1, skipped=0, failed=0, apps=1)
    assert (tmp_path / "archive/v1/10_10_100_555.shas").read_text() == f"{_SHA_A}\n{_SHA_B}\n"


def test_an_already_archived_manifest_is_skipped(tmp_path):
    _setup(tmp_path, [10])
    v1 = tmp_path / "archive/v1"
    v1.mkdir(parents=True)
    (v1 / "10_10_100_555.shas").write_text(f"{_SHA_A}\n")
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_555.manifest"]}, _DONE],
        manifests={10: {"100_555.manifest": [_SHA_B]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=0, skipped=1, failed=0, apps=1)
    assert (v1 / "10_10_100_555.shas").read_text() == f"{_SHA_A}\n"


def test_per_app_failures_are_counted_with_their_reason(tmp_path):
    _setup(tmp_path, [10, 20, 30, 40])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "not_owned", "reason": "no access to 1 depot(s)"},
            {"app": 30, "status": "no_depots", "reason": "no Windows 64-bit English depots"},
            {"app": 40, "status": "error", "reason": "depot 41: CDN returned 404"},
            _DONE,
        ],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    with capture_logs() as logs:
        result = _fetcher(tmp_path).fetch_all()
    assert result == FetchResult(fetched=1, skipped=0, failed=3, apps=4)
    reasons = {e.get("app_id"): e.get("reason") for e in logs if e["event"] == "manifest_fetch.app_failed"}
    assert reasons[40] == "depot 41: CDN returned 404"
    assert reasons[20] == "no access to 1 depot(s)"


def test_a_refused_login_raises_with_steams_reason(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"summary": True, "session": "login_refused", "reason": "Steam refused the login: RateLimitExceeded"}],
        exit_code=2,
    )
    with pytest.raises(HelperSessionError, match="RateLimitExceeded"):
        _fetcher(tmp_path).fetch_all()


def test_a_stopped_session_archives_what_it_fetched_before_raising(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "not_attempted", "reason": "connection lost twice"},
            {"summary": True, "session": "disconnected", "reason": "connection lost twice: NoConnection"},
        ],
        exit_code=4,
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    with pytest.raises(HelperSessionError, match="not_attempted=1"):
        _fetcher(tmp_path).fetch_all()
    assert (tmp_path / "archive/v1/10_10_100_1.shas").exists()


def test_a_failed_session_import_raises_naming_the_login_command(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"summary": True, "session": "import_failed", "reason": "no saved login. Run: SteamManifestHelper login --username kraulerson --session-dir /x"}],
        exit_code=3,
    )
    with pytest.raises(HelperSessionError, match="SteamManifestHelper login"):
        _fetcher(tmp_path).fetch_all()


def test_a_hung_helper_is_killed(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(tmp_path, lines=[_DONE], sleep=10.0)
    with pytest.raises(RuntimeError, match="timed out"):
        _fetcher(tmp_path, timeout_sec=1.0).fetch_all()


def test_a_stray_non_json_stdout_line_is_ignored(tmp_path):
    # Review Focus 4.
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_1.manifest"]}, _DONE],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
        extra_stdout="SteamKit2 debug: something happened",
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=0, apps=1)


def test_an_unexpected_exit_without_a_summary_raises_with_the_stderr_tail(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(tmp_path, lines=[], exit_code=1)
    with pytest.raises(RuntimeError, match="helper log line"):
        _fetcher(tmp_path).fetch_all()


def test_ok_with_no_readable_manifest_counts_as_failed(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "ok", "manifests": ["200_2.manifest"]},
            _DONE,
        ],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=1, apps=2)


def test_a_manifest_name_outside_the_pattern_is_never_read(tmp_path):
    # App 10 lists a traversal name; app 20 is good, so the run doesn't hit the
    # all-failed rule and the assertion is about the name alone.
    _setup(tmp_path, [10, 20])
    (tmp_path / "secret.manifest").write_bytes(_manifest_bytes([_SHA_B]))
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["../../../secret.manifest"]},
            {"app": 20, "status": "ok", "manifests": ["200_2.manifest"]},
            _DONE,
        ],
        manifests={20: {"200_2.manifest": [_SHA_A]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=1, apps=2)
    assert [p.name for p in (tmp_path / "archive" / "v1").glob("*.shas")] == ["20_20_200_2.shas"]


def test_total_failure_still_raises(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "error", "reason": "x"}, {"app": 20, "status": "error", "reason": "y"}, _DONE],
    )
    with pytest.raises(RuntimeError, match="all 2 apps"):
        _fetcher(tmp_path).fetch_all()


def test_a_helper_session_counts_as_a_session(tmp_path):
    cfg = tmp_path / "dd-config"
    (cfg / "steam-manifest-helper").mkdir(parents=True)
    (cfg / "steam-manifest-helper" / "session.json").write_text("{}")
    _fetcher(tmp_path).login_from_session()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/platform/steam/test_manifest_fetcher.py -q 2>&1 | tail -3
```

Expected: a collection error, `ImportError: cannot import name 'HelperSessionError'`.

- [ ] **Step 3: Rewrite the fetcher**

In `src/orchestrator/platform/steam/manifest_fetcher.py`:

1. Replace the module docstring with:

```python
"""SteamManifestFetcher: fetch Steam manifests ONLY (no chunks) through the
one-login SteamManifestHelper (#361), writing {app}_{app}_{depot}_{gid}.shas
sidecars into the durable manifest archive so the F7 validator covers apps
SteamPrefill skips. STDLIB + subprocess only; MUST NOT import orchestrator.api.* /
orchestrator.db.* (agent import-isolation, tests/agent/test_import_isolation.py).
NEVER sees, logs or writes the Steam token. Only the helper reads it."""
```

2. Remove `import time`, `_TRANSIENT_RE`, `_MAX_BACKOFF_SEC` and `TransientFetchError`.

3. Add, after `class SteamAuthError`:

```python
class HelperSessionError(RuntimeError):
    """The helper's Steam session stopped early: login refused (exit 2), session
    import failed (exit 3), or the connection dropped twice (exit 4). Raised AFTER
    the manifests it did fetch are archived, so partial progress is kept."""


_HELPER_STOPS = {2: "login refused", 3: "session import failed", 4: "connection lost"}
_MANIFEST_NAME_RE = re.compile(r"(?P<depot>\d+)_(?P<gid>\d+)\.manifest")


@dataclass(frozen=True)
class _HelperRun:
    returncode: int
    apps: list[dict[str, object]]
    summary: dict[str, object] | None
    stderr_tail: str
```

4. Replace the whole `class DepotDownloaderManifestFetcher` with the class below. `_enumerate_app_ids`, `_app_ids_with_ext` and `_write_shas` are copied **unchanged** from the old class.

```python
class SteamManifestFetcher:
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        steam_config_dir: Path,
        archive_dir: Path,
        username: str = "",
        timeout_sec: float = 7200.0,
        manifest_cache_dir: Path | None = None,
    ) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)
        self._steam_config_dir = Path(steam_config_dir)
        self._archive_dir = Path(archive_dir)
        self._username = username
        self._timeout_sec = timeout_sec
        self._manifest_cache_dir = Path(manifest_cache_dir) if manifest_cache_dir else None
        # The helper keeps its own session here, imported once from DepotDownloader's.
        self._session_dir = self._config_dir / "steam-manifest-helper"

    def login_from_session(self) -> None:
        """A run can proceed if the helper already holds a session, or if
        DepotDownloader left one it can import. Neither: SteamAuthError, so the
        caller surfaces 're-auth needed' instead of prompting in an unattended run."""
        if (self._session_dir / "session.json").exists():
            return
        if any(self._config_dir.glob(_SESSION_GLOB)):
            return
        raise SteamAuthError(
            "no Steam session: run SteamManifestHelper login --username <user> "
            f"--session-dir {self._session_dir}"
        )

    # _enumerate_app_ids, _app_ids_with_ext, _write_shas: unchanged from the old class.

    def _run_helper(self, app_ids: list[int], scratch: Path) -> _HelperRun:
        apps_file = scratch / "apps.txt"
        apps_file.write_text("".join(f"{a}\n" for a in app_ids))
        argv = [
            str(self._binary), "fetch",
            "--apps", str(apps_file),
            "--out", str(scratch / "manifests"),
            "--session-dir", str(self._session_dir),
            "--username", self._username,
            "--import-from", str(self._config_dir),
        ]
        try:
            proc = subprocess.run(  # noqa: S603  argv list, no shell
                argv, capture_output=True, text=True, timeout=self._timeout_sec
            )
        except subprocess.TimeoutExpired as e:
            # TimeoutExpired carries bytes even with text=True.
            err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            raise RuntimeError(
                f"steam manifest helper timed out after {self._timeout_sec:.0f}s; stderr tail: {err[-300:]}"
            ) from e
        apps: list[dict[str, object]] = []
        summary: dict[str, object] | None = None
        for line in proc.stdout.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # a stray non-JSON line is not data
            if not isinstance(obj, dict):
                continue
            if obj.get("summary") is True:
                summary = obj
            elif "app" in obj and "status" in obj:
                apps.append(obj)
        return _HelperRun(proc.returncode, apps, summary, (proc.stderr or "")[-500:])

    def _archive_app(self, app_id: int, app_dir: Path, names: object) -> tuple[int, int] | None:
        """Archive one ok app's manifests. Returns (fetched, skipped), or None when
        the helper listed no readable manifest for it. Names must match
        <depot>_<gid>.manifest exactly, so a listed name can never escape app_dir."""
        if not isinstance(names, list):
            return None
        fetched = skipped = 0
        seen = False
        for name in names:
            if not isinstance(name, str):
                continue
            match = _MANIFEST_NAME_RE.fullmatch(name)
            if match is None:
                continue
            path = app_dir / name
            if not path.is_file():
                continue
            seen = True
            shas = parse_steamkit_manifest(path.read_bytes())
            if self._write_shas(app_id, int(match["depot"]), match["gid"], shas):
                fetched += 1
            else:
                skipped += 1
        return (fetched, skipped) if seen else None

    def fetch_all(self) -> FetchResult:
        """One run: verify a session exists, enumerate the cached app set, run the
        helper ONCE for all of it, and archive .shas sidecars. Per-app failures are
        counted; a session that stopped early raises AFTER archiving what it got."""
        self.login_from_session()
        app_ids = self._enumerate_app_ids()
        fetched = skipped = failed = not_attempted = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                scratch = Path(tmp)
                run = self._run_helper(app_ids, scratch)
                for line in run.apps:
                    app_id, status = line.get("app"), line.get("status")
                    if status == "not_attempted":
                        not_attempted += 1
                        continue
                    written = (
                        self._archive_app(app_id, scratch / "manifests" / str(app_id), line.get("manifests"))
                        if status == "ok" and isinstance(app_id, int)
                        else None
                    )
                    if written is None:
                        failed += 1
                        reason = line.get("reason") or "helper reported ok but produced no manifest"
                        _log.warning("manifest_fetch.app_failed", app_id=app_id, status=status, reason=str(reason)[:200])
                        continue
                    fetched += written[0]
                    skipped += written[1]
        except BaseException as e:  # ③: a timeout-style escape must not kill the agent silently
            _log.error("manifest_fetch.run_aborted", reason=f"{type(e).__name__}: {e}"[:200])
            raise
        _log.info(
            "manifest_fetch.done",
            apps=len(app_ids), fetched=fetched, skipped=skipped, failed=failed,
            not_attempted=not_attempted, helper_exit=run.returncode,
        )
        if run.returncode in _HELPER_STOPS:
            reason = str((run.summary or {}).get("reason") or _HELPER_STOPS[run.returncode])
            raise HelperSessionError(
                f"steam manifest helper stopped ({_HELPER_STOPS[run.returncode]}): {reason}"
                f" | fetched={fetched} skipped={skipped} failed={failed} not_attempted={not_attempted}"
            )
        if run.returncode != 0 or run.summary is None:
            raise RuntimeError(
                f"steam manifest helper failed (exit {run.returncode}); stderr tail: {run.stderr_tail[-300:]}"
            )
        if fetched == 0 and skipped == 0 and failed > 0:
            raise RuntimeError(f"manifest fetch failed for all {failed} apps")
        return FetchResult(fetched=fetched, skipped=skipped, failed=failed, apps=len(app_ids))
```

- [ ] **Step 4: Run the tests to verify they pass, then lint and type-check**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/platform/steam/test_manifest_fetcher.py tests/agent/test_import_isolation.py -q 2>&1 | tail -3
.venv/bin/ruff check src/orchestrator/platform/steam/manifest_fetcher.py tests/platform/steam/test_manifest_fetcher.py && .venv/bin/ruff format src/orchestrator/platform/steam/manifest_fetcher.py tests/platform/steam/test_manifest_fetcher.py
.venv/bin/mypy src/orchestrator/platform/steam/manifest_fetcher.py
```

Expected: all pass; ruff and mypy clean. `tests/agent/test_steam.py` and `app.py` still reference the old class: Task 8 fixes them, so run only these two files now.

- [ ] **Step 5: No commit here. Go straight on to Task 8**

`.git/hooks/pre-commit` runs `mypy --strict src/` over the whole tree whenever a `.py` file is staged. Until Task 8 rewires it, `app.py` still imports the removed `DepotDownloaderManifestFetcher`, so a commit made here would be blocked. Tasks 7 and 8 are therefore one unit with one commit, made at Task 8 Step 6.

---

### Task 8: Settings and agent wiring

**Files:**
- Modify: `src/orchestrator/core/settings.py` lines 134-155
- Modify: `src/orchestrator/agent/app.py` lines 26, 79-89, 139-149
- Test: `tests/core/test_settings.py` lines 762-778

**Interfaces:**
- Consumes: `SteamManifestFetcher` (Task 7).
- Produces:
  - `Settings.steam_manifest_helper_binary: Path = Path("/steam-manifest-helper/SteamManifestHelper")`
  - `Settings.manifest_fetch_timeout_sec: float = 7200.0`
- Removed: `depotdownloader_binary`, `manifest_fetch_delay_sec`, `manifest_fetch_max_retries`, `manifest_fetch_retry_backoff_sec`. Settings use `extra="ignore"`, so an env file that still sets them keeps working.

- [ ] **Step 1: Write the failing test**

Replace `test_manifest_fetcher_settings_defaults` and `test_manifest_fetcher_settings_env_override` in `tests/core/test_settings.py` with:

```python
def test_manifest_fetcher_settings_defaults():
    s = Settings(orchestrator_token="a" * 32)
    assert s.steam_manifest_helper_binary == Path("/steam-manifest-helper/SteamManifestHelper")
    assert s.depotdownloader_config_dir == Path("/depotdownloader-config")
    # #361: one login per run replaced the per-app delay and retry settings.
    assert s.manifest_fetch_timeout_sec == 7200.0
    assert not hasattr(s, "manifest_fetch_delay_sec")


def test_manifest_fetcher_settings_env_override(monkeypatch):
    monkeypatch.setenv("ORCH_MANIFEST_FETCH_TIMEOUT_SEC", "60")
    monkeypatch.setenv("ORCH_DEPOTDOWNLOADER_CONFIG_DIR", "/custom/dd")
    # A removed setting left in an env file must not break boot (extra="ignore").
    monkeypatch.setenv("ORCH_MANIFEST_FETCH_DELAY_SEC", "8")
    s = Settings(orchestrator_token="a" * 32)
    assert s.manifest_fetch_timeout_sec == 60.0
    assert s.depotdownloader_config_dir == Path("/custom/dd")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/core/test_settings.py -q -k manifest_fetcher 2>&1 | tail -3
```

Expected: `AttributeError: 'Settings' object has no attribute 'steam_manifest_helper_binary'`.

- [ ] **Step 3: Edit the settings**

Replace lines 134-155 of `src/orchestrator/core/settings.py`: from the comment above `depotdownloader_binary` through `manifest_fetch_retry_backoff_sec`. Keep the comment block above line 134 that introduces the manifest fetcher, and read it first. The replacement:

```python
    # #361: the one-login Steam manifest helper (C#, SteamKit2 3.4.0), replacing
    # the per-app DepotDownloader invocation that logged in 1,211 times a run.
    # Self-contained .NET 10 build; writes manifests the fetcher turns into .shas.
    steam_manifest_helper_binary: Path = Path("/steam-manifest-helper/SteamManifestHelper")
    # Persistent mount holding the Steam session. The helper keeps its own
    # session.json under <dir>/steam-manifest-helper/, imported once from the
    # DepotDownloader login already saved here.
    depotdownloader_config_dir: Path = Path("/depotdownloader-config")
    # Steam account username. Not a secret: the helper takes it as --username to
    # pick the right saved login. Required for manifest fetching.
    steam_username: str = ""
    # Whole-run ceiling for the helper (#361). A hung helper is killed and the job
    # fails. A setting, not a constant, so it is not another #315.
    manifest_fetch_timeout_sec: float = Field(default=7200.0, gt=0.0)
```

- [ ] **Step 4: Edit the agent wiring**

In `src/orchestrator/agent/app.py`, change the import on line 26 to:

```python
from orchestrator.platform.steam.manifest_fetcher import SteamManifestFetcher
```

Replace **both** construction blocks (around lines 79-89 and 139-149) with the same call. Keep each block's existing indentation and its `app.state.manifest_fetcher =` assignment:

```python
SteamManifestFetcher(
    binary=settings.steam_manifest_helper_binary,
    config_dir=settings.depotdownloader_config_dir,
    steam_config_dir=settings.steam_prefill_config_dir,
    archive_dir=settings.steam_manifest_archive_dir,
    username=settings.steam_username,
    timeout_sec=settings.manifest_fetch_timeout_sec,
    manifest_cache_dir=settings.steam_manifest_cache_dir,
)
```

- [ ] **Step 5: Run the full suite, lint and type-check**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3
.venv/bin/ruff check src tests && .venv/bin/mypy src
```

Expected: every test passes, ruff and mypy are clean, and nothing references the removed names:

```bash
grep -rnE "DepotDownloaderManifestFetcher|TransientFetchError|manifest_fetch_delay_sec|manifest_fetch_max_retries|manifest_fetch_retry_backoff_sec|depotdownloader_binary" src tests --include='*.py' | grep -v tests/uat/ ; echo "matches above must be none"
```

Then mark the build-loop step:

```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
```

- [ ] **Step 6: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Tasks 7 and 8 of the approved 361 plan: the fetcher calls the helper once, plus settings and agent wiring"
```

```bash
git add src/orchestrator/platform/steam/manifest_fetcher.py tests/platform/steam/test_manifest_fetcher.py src/orchestrator/core/settings.py src/orchestrator/agent/app.py tests/core/test_settings.py
git status --short
git commit -m "feat(steam): fetch manifests through the one-login helper and retire DepotDownloader settings (#361)"
```

---

### Task 9: Security audit, documentation, feature record

**Files:**
- Create: `docs/security-audits/steam-manifest-helper-security-audit.md`
- Create: `docs/ADR documentation/0019-steam-manifest-helper.md`
- Modify: `docs/deploy/INSTALL.md`: add the helper login step after the `docker volume create depotdownloader-config` line (~98).
- Modify: `docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md`: append the three refinements from Global Constraints.
- Modify: `CHANGELOG.md`: a new top entry under `[Unreleased]`.
- Modify: `FEATURES.md`: append `## Feature 30`.
- Modify: `CLAUDE.md`: Current State gets one bullet for #361.

- [ ] **Step 1: Run the audit checks and write the audit**

Run each check. Paste its real output into the audit under its heading.

```bash
# 1. The token never reaches output, logs or argv: no write call names it.
grep -nE "Write(Line)?\(.*(RefreshToken|AccessToken|Token)" tools/steam_manifest_helper/SteamManifestHelper/*.cs; echo "matches above must be none"
grep -n "RefreshToken" src/orchestrator -r; echo "matches above must be none (Python never sees the token)"
# 2. Subprocess is an argv list, no shell, and has a timeout.
grep -n "subprocess.run" -A3 src/orchestrator/platform/steam/manifest_fetcher.py
# 3. Manifest names can't escape the scratch dir (pattern + test).
grep -n "_MANIFEST_NAME_RE" src/orchestrator/platform/steam/manifest_fetcher.py
# 4. Supply chain: exact NuGet pins, lock files, SDK digest.
grep -n "Version=" tools/steam_manifest_helper/*/*.csproj; ls tools/steam_manifest_helper/*/packages.lock.json; grep -n "dotnet/sdk" Dockerfile
# 5. semgrep and gitleaks over the branch.
semgrep --config .semgrep src tools 2>&1 | tail -3; gitleaks detect --no-banner --log-opts="origin/main..HEAD" 2>&1 | tail -2
```

Write `docs/security-audits/steam-manifest-helper-security-audit.md` in the format of `docs/security-audits/keys-zone-alarm-security-audit.md`:
- **Scope:** the helper, the fetcher rewrite and the Dockerfile.
- **Threat model:**
  - the Steam refresh token at rest (`session.json` 0600) and in memory;
  - helper stdout as untrusted input to Python;
  - the new NuGet supply chain;
  - LGPL compliance.
- One section per check above, holding its real output and a verdict.
- **Residual risks:**
  - the token sits in plain JSON on the persistent mount, as DepotDownloader's did;
  - `login` reads the password from the console, so it needs a TTY.

Then mark the step:

```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```

- [ ] **Step 2: Write the ADR**

`docs/ADR documentation/0019-steam-manifest-helper.md`. Follow the ADR template in that folder (read `0016-ownership-as-an-explicit-input.md` for the house shape). Fill each section as follows:
- **Status:** Accepted, 2026-10-07.
- **Context:** #361's root cause. One DepotDownloader login per app (1,211 a run). The #228 back-off read stderr while DepotDownloader writes to stdout: 0 `transient_retry` across 729 failures.
- **Decision:** the decisions table from the spec.
- **Alternatives:**
  - contain-then-fix (stop on refusal; rotate the order);
  - adapting DepotDownloader's code (GPL-2.0);
  - keeping both tools behind a switch (the 250 MiB CI limit).
- **Consequences:**
  - one login per run;
  - a new .NET component in the image;
  - the `login` command for token renewal;
  - rollback is by image tag.

- [ ] **Step 3: Update `INSTALL.md`**

After the `docker volume create depotdownloader-config` line, add:

```markdown
**Steam login for manifest fetching (one time).** The weekly manifest fetch logs
in through `SteamManifestHelper` with a saved Steam session. An install that
already has a DepotDownloader login in this volume needs nothing: the helper
imports it on its first run. On a fresh install, or when a run fails with
"Steam rejected the saved login", log in once. You'll type the password, then
approve the login in the Steam app:

    docker exec -it -u 0 orchestrator-agent /steam-manifest-helper/SteamManifestHelper \
        login --username <steam-user> --session-dir /depotdownloader-config/steam-manifest-helper

Set `ORCH_STEAM_USERNAME=<steam-user>` in the agent's env file as well.
```

Before writing it, confirm the command shape against the built helper. Run it with no arguments; it should print the usage text, which is Task 5's `Cli.Usage`.

- [ ] **Step 4: Spec refinements, CHANGELOG, FEATURES, CLAUDE.md**

Append a `## Refinements made while planning (2026-10-07)` section to the spec, holding the three numbered refinements from this plan's Global Constraints, word for word.

Add a CHANGELOG entry at the top of `[Unreleased]`, in the house style: a heading, then bullets with the key claim in bold.
- **Heading:** `### Changed — the weekly Steam manifest fetch logs in once, not 1,211 times (#361) — <date>`.
- **Bullets:**
  - the root cause;
  - the helper;
  - Python calls it once;
  - DepotDownloader removed;
  - the `login` command;
  - rollback by tag.

Append `## Feature 30: One-Login Steam Manifest Helper (#361)` to `FEATURES.md` in the format of Feature 29: status, what it does, files, tests, decisions.

Add one Current State bullet to `CLAUDE.md`, saying #361 is built and naming the spec and plan.

```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```

- [ ] **Step 5: Record the feature**

```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
scripts/test-gate.sh --record-feature "steam-manifest-helper"
```

- [ ] **Step 6: Commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "Task 9 of the approved 361 plan: security audit, ADR 0019, install guide, changelog and feature record"
```

```bash
git add "docs/security-audits/steam-manifest-helper-security-audit.md" "docs/ADR documentation/0019-steam-manifest-helper.md" docs/deploy/INSTALL.md docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md CHANGELOG.md FEATURES.md CLAUDE.md .claude/process-state.json .claude/build-progress.json
git status --short
git commit -m "docs(steam): security audit, ADR 0019, install and feature record for the helper (#361)"
```

---

### Task 10: Review, merge, deploy, first live run

This task runs outside a scratch branch, against live systems. Each step that changes a host needs a change card printed first, and Karl's approval where `/change-card` says so.

**Files:** none. Operational only.

- [ ] **Step 1: One adversarial review, then the PR**

Dispatch one fresh `pr-adversary` agent on `opus`, at max effort, against the pushed branch. It must:
- reproduce every claim, in a fresh clone;
- run `dotnet test` and the full pytest suite;
- build the image and check its size.

Fix the reachable findings test-first. Record the rest as residual issues. Then open the PR with `Refs #361` (not `Fixes`: #361 closes on Monday evidence) and watch CI to green:

```bash
gh pr checks <N> --watch && gh pr view <N> --json state,mergeStateStatus
```

Merge with `gh pr merge <N> --merge` only on Karl's go.

- [ ] **Step 2: Build on the LXC and tag the rollback image on both hosts**

Follow `docs/deploy/` and the deploy memory: build on the LXC clone after `git pull`, then stream the image to the NAS via the Mac. First:

```bash
ssh root@10.100.23.105 'docker tag orchestrator:dpa orchestrator:dpa-pre-361'
ssh karl@192.168.1.30 'docker tag orchestrator:dpa orchestrator:dpa-pre-361'
```

- [ ] **Step 3: Recreate the agent in an inter-sweep gap**

The agent's validate calls serve the running sweep, so check the queue first:

```bash
ssh root@10.100.23.105 'docker exec orchestrator python3 /tmp/qjobs.py'
```

Wait for no `running`/`queued` sweep, print the change card, and recreate the agent with its compose file. It must run `--user 0:0`. Then verify:
- the helper is present;
- the agent runs as uid 0;
- 256/256 cache buckets are visible.

```bash
ssh karl@192.168.1.30 'docker exec orchestrator-agent /steam-manifest-helper/SteamManifestHelper; echo exit=$?; docker exec orchestrator-agent id -u'
```

Expected: the usage text, `exit=64`, and `0`. Then run the existing 256-bucket check from the agent-recreate runbook.

- [ ] **Step 4: First real run, by hand, in a gap away from SteamPrefill ticks**

```bash
ssh root@10.100.23.105 'docker exec orchestrator orchestrator-cli cache fetch-manifests'
```

Watch the job to completion. Expected:
- `manifest_fetch.done` has `"logons": 1` (the helper's own count of its
  connect-and-logon calls; 2 means it reconnected once). Do not grep for
  `logged on as`: that line is printed twice per logon, and the helper's stderr
  is folded into one `stderr_tail` field, so it cannot count logons (final
  review M2);
- `manifest_fetch.done` has `failed` near 0 and `not_attempted=0`;
- no `manifest_fetch.app_failed` stream.

```bash
ssh karl@192.168.1.30 'docker logs --since 6h orchestrator-agent 2>&1 | grep "\"manifest_fetch.done\"" | tail -1 | grep -oE "\"(logons|failed|not_attempted)\": [0-9]+"'
```

- [ ] **Step 5: Parity on live data**

Run the helper directly for 50 apps that already have `.shas`, using Task 1's `apps.txt` method, into `/tmp/parity361`. Then run Task 1's `parity.py`, with `root` set to `/tmp/parity361`. Expected: `different=0`. Clean up `/tmp/parity361` afterwards.

- [ ] **Step 6: Monday evidence, then close #361**

On the first Monday after deploy, check all four:

```bash
ssh karl@192.168.1.30 'grep -E "^20[0-9-]+T06:0" /home/karl/lancache-host/steamprefill-cache/cron.log | tail -4; grep -c RateLimitExceeded /home/karl/lancache-host/steamprefill-cache/cron.log'
ssh root@10.100.23.105 'docker exec -i orchestrator python -' <<'PY'
import sqlite3
c = sqlite3.connect("/var/lib/orchestrator/orchestrator.db")
for r in c.execute("SELECT id, state, started_at, finished_at FROM jobs WHERE kind='fetch_manifests' ORDER BY id DESC LIMIT 2"):
    print(r)
PY
```

Expected:
- the 06:00 MDT tick ends `END steam prefill ok`;
- the `RateLimitExceeded` count is unchanged from before the deploy;
- the latest `fetch_manifests` job `succeeded`;
- Kuma 176 is UP.

Comment the evidence on #361 and close it.
