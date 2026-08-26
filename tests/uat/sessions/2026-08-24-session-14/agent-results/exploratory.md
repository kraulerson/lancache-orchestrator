# UAT-14 — Exploratory test (Phase 2.7 persona: Malicious User)

**Date:** 2026-08-24
**Scope:** local instances only. Control-plane API on `127.0.0.1:8791`, data-plane agent on
`127.0.0.1:8792`, throwaway SQLite DB and cache/manifest roots under the session scratchpad.
**The live deployment (10.100.23.105 / 192.168.1.40 / the NAS agent) was never touched.**
**Repo state:** no tracked file modified. `git status --short` shows only the two `.claude/*`
state files that were already dirty at session start, plus this untracked session directory.

---

## Summary

Roughly 13,000 fuzzed requests plus targeted hand-built attacks. The parts that have been
hardened before held up extremely well: **zero 5xx across 12,614 fuzzed GETs** against every
paginated/filtered read endpoint, no SQL injection, no ORDER BY injection, no LIKE-pattern
abuse, no path traversal into the cache root, no auth bypass, no source-IP spoof, and the
`#263` / `#264` / `#265` fixes all behave correctly end-to-end including through the CLI.

The damage is concentrated in two places that the API's own hardening never reached:

1. **The validator calls a game "cached" when it has nothing to check.** A manifest that yields
   zero chunks — a zero-byte `.bin`, a truncated `.bin`, an empty `.shas`, or a well-formed Epic
   manifest with an empty `ChunkHashList` — returns `outcome: "cached"`, `chunks_total: 0`,
   `error: null`, which the control plane maps to `games.status = 'up_to_date'`. And the manifest
   archive can produce exactly that state permanently: it copies non-atomically to the final
   filename, then skips by filename forever. A game can therefore sit green in Game_shelf with
   *nothing* in lancache, and the 6-hourly sweep will keep re-confirming it. This defeats the
   product's stated purpose.
2. **The data-plane agent is missing three protections the API has**: no body-size cap (a 238 MB
   request pushed agent RSS from 63 MB to 698 MB and blocked its event loop for 88 s), no
   `RequestValidationError` handler (deep JSON → 500 via `RecursionError`), and an uncaught
   `OSError` on a long path component (500). The agent is the LAN-exposed process that sits
   beside lancache on the CPU-steal-bound NAS.

Confirmed: **2 SEV-2, 9 SEV-3**. The suspected uvicorn `proxy_headers` weakness is **real as a
mechanism but not exploitable in the current deployment shape** — details in SEV-3-9 and in
"Attacked and held".

---

## Confirmed defects

### SEV-2-1 — A zero-chunk manifest reports the game as fully cached (false green), and the archive can make it permanent

**What happens.** `agent/routers/steam.py::_steam_chunk_paths` counts a manifest as
successfully parsed (`parsed_ok += 1`) even when the parse produces **zero** chunk SHAs.
`steam_validate` then falls into the `included == 0` branch, computes `union_total = 0`, and
returns `"missing" if union_total else "cached"` → **`"cached"`**. `agent/routers/epic.py`
has the same shape explicitly (`if total == 0: ... "outcome": "cached"`).
`jobs/handlers/validate.py` maps `"cached"` → `games.status = 'up_to_date'`.

**Reproduction (all four ran against the local agent + API):**

```bash
# Steam — a zero-byte .bin is enough
: > "$SCRATCH/spcache/v1/440_440_441_1111.bin"
curl -s -X POST -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
     -d '{"app_id":440}' http://127.0.0.1:8792/v1/steam/validate
# {"chunks_total":0,"chunks_cached":0,"chunks_missing":0,"outcome":"cached","versions":"441:1111","error":null}

# Steam — 3 bytes of garbage
printf '\x1b\xff\xff' > "$SCRATCH/spcache/v1/730_730_731_2222.bin"   # -> outcome "cached"

# Steam — empty .shas sidecar
: > "$SCRATCH/spcache/v1/570_570_571_3333.shas"                      # -> outcome "cached"

# Epic — well-formed manifest, empty ChunkHashList
M=$(python3 -c "import base64,json;print(base64.b64encode(json.dumps(
   {'ManifestFileVersion':'000000000000000000','ChunkHashList':{},'DataGroupList':{}}).encode()).decode())")
curl -s -X POST -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"app_id\":1,\"version\":\"v\",\"cdn_base\":\"/x\",\"raw_manifest_b64\":\"$M\"}" \
  http://127.0.0.1:8792/v1/epic/validate
# {"chunks_total":0,"chunks_cached":0,"chunks_missing":0,"outcome":"cached","versions":"0","error":null}
```

**End-to-end through the control plane** (agent enabled, game 1 = steam app 440, whose only
manifest on disk is the zero-byte `.bin` above):

```bash
curl -s -X POST -H "Authorization: Bearer $T" http://127.0.0.1:8791/api/v1/games/1/validate
# {"job_id":8}
sqlite3 orch.db "SELECT id,title,status FROM games WHERE id=1;
                 SELECT game_id,chunks_total,chunks_cached,outcome,error FROM validation_history;"
# 1|Team Fortress 2|up_to_date
# 1|0|0|cached|
```

`status='up_to_date'`, `outcome='cached'`, `error=NULL`. Nothing anywhere says "I checked zero
chunks". Game_shelf renders a green Cached badge (`chunks_cached`/`chunks_total` are both 0, so
it is not even a "Partial · N%").

**Why a truncated manifest is a realistic live state — and permanent.**
`agent/manifest_archive.py::sync_manifests_to_archive` does
`shutil.copy2(src, archive_v1 / src.name)` — writing **directly to the final archive filename**,
with no temp-file + rename. If the agent container is killed mid-copy (every redeploy does
exactly this) or the volume fills, a partial file is left at the final name. The next sync builds
`existing = {p.name for p in archive_v1.glob("*.bin")}` and does `if src.name in existing:
continue` — so the truncated file is **never re-copied**, even though a complete source still
sits in the live cache. Proven:

```python
from orchestrator.agent.manifest_archive import sync_manifests_to_archive
good = live/"v1"/"440_440_441_9.bin"; good.write_bytes(b"...2100 bytes...")   # complete source
(arch/"v1"/"440_440_441_9.bin").write_bytes(b"")                              # interrupted copy
sync_manifests_to_archive(live, arch)
# archive file size BEFORE sync: 0
# copied by sync: 0
# archive file size AFTER  sync: 0 (live source is 2100 bytes)
```

Because the archive is append-only and `locate_manifest_bins` picks newest-by-mtime per depot,
that 0-byte file is the manifest of record for the depot from then on.

**Impact.** A game reports `up_to_date` with an empty cache, forever. The scheduled sweep
re-confirms it every cycle. Operators and Game_shelf both read green. Purge on the same game
returns `{"deleted": 0}` and looks like a no-op. This is the exact failure the orchestrator
exists to detect. Two independent bugs (classification + archive) compound into a silent,
self-perpetuating false green.

**Suggested direction (not implemented).** A parsed-but-empty manifest is an `error`
(`"manifest_empty"`), not `"cached"`; `outcome: "cached"` with `chunks_total == 0` should never
set `up_to_date`. Separately, copy to `name.tmp` and `os.replace` into place, and treat a
0-byte archive entry as absent.

---

### SEV-2-2 — The data-plane agent enforces no request-body size cap; one request drove RSS from 63 MB to 698 MB and blocked it for 88 s

**What happens.** `api/main.py::create_app` installs `BodySizeCapMiddleware` (32 KiB, Bible
§9.2). `agent/app.py::create_agent_app` installs only `BearerAuthMiddleware` and
`SourceAllowlistMiddleware` — the body cap was never carried across. None of the agent request
models bound their collections either: `StatRequest.hashes: list[str]`,
`PullRequest.chunks: list[_ChunkIn]`, `SteamPrefillRequest.app_ids`, and the Epic routes'
`raw_manifest_b64: str` all have no `max_length`.

**Reproduction:**

```bash
# API rejects a 42 KB body
curl -o /dev/null -w "%{http_code}\n" -X PUT -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' --data-binary @body42k.json \
  http://127.0.0.1:8791/api/v1/prefill-exclusions/gameshelf/steam
# 413

# Agent accepts a 238 MB body
python3 -c 'import sys;sys.stdout.write("{\"hashes\": [" + ",".join(["\"" + "a"*32 + "\""]*7000000) + "]}")' > huge.json
ps -o rss= -p $AGENT_PID          # 63728  KB
time curl -o out -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' --data-binary @huge.json http://127.0.0.1:8792/v1/stat
# 200          1:28.29 total
ps -o rss= -p $AGENT_PID          # 698672 KB   (and it did not return afterwards)
cat out                           # {"cached":0,"missing":7000000}
```

238 MB of body → **~635 MB of resident memory** (JSON text + 7 M Python `str` + 7 M `Path`
objects) and **88 seconds** in which the agent's single uvicorn listener served nothing and the
dedicated cache-stat executor was fully occupied. RSS did not come back down after the request
completed.

**Impact.** The agent runs on the UGREEN NAS beside lancache, on a host the project's own notes
describe as 4-core-capped and CPU-steal-bound. A caller who has the bearer and an allowlisted
source IP (Game_shelf holds both; the status page puts the bearer in browser sessionStorage) can
OOM the agent container or wedge it at will — and a wedged agent is precisely the condition UAT-12
added connect-retries for, and the condition that makes the control plane report "agent
unreachable" and discard sweep work. It is a post-auth DoS, but it is a one-request DoS against
the machine that also serves the LAN's game cache.

---

### SEV-3-1 — Agent returns HTTP 500 on a launcher name of 256+ bytes; the API relabels it "agent unavailable"

`agent/routers/manual_downloads.py` validates the launcher against
`^[A-Za-z0-9 ._-]+$` with **no length bound**, then calls `target.is_dir()`. At 256 bytes the
filesystem raises `OSError: [Errno 63] File name too long`, which nothing catches.

```bash
L=$(python3 -c "print('A'*256)")
curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
     "http://127.0.0.1:8792/v1/manual-downloads/$L"     # 500  Internal Server Error
curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
     "http://127.0.0.1:8791/api/v1/manual-downloads/$L" # 503  {"detail":"agent unavailable"}
# 255 bytes -> 200 on both.  Threshold is NAME_MAX, so it reproduces on ext4/btrfs too.
```

Agent log: `OSError: [Errno 63] File name too long: '.../manual/AAAA…'`.

Two defects in one: (a) an unhandled 5xx from trivially-reachable input — the same class #263
was filed for; (b) the control-plane proxy's `except Exception` turns **every** agent 4xx/5xx
into `503 {"detail": "agent unavailable"}`. Confirmed separately with `..`:

```bash
curl --path-as-is -H "Authorization: Bearer $T" http://127.0.0.1:8792/v1/manual-downloads/..
# 400 {"detail":"invalid launcher"}          <- agent is healthy and answering correctly
curl --path-as-is -H "Authorization: Bearer $T" http://127.0.0.1:8791/api/v1/manual-downloads/..
# 503 {"detail":"agent unavailable"}         <- control plane says the agent is down
```

**Impact.** A bad request is reported as an infrastructure outage. An operator debugging this
goes to the NAS to check a healthy agent; Game_shelf's retry logic retries a permanently-invalid
input forever; 503s from this route are worthless as an agent-health signal.
The API's own comment claims its regex is the *"Same allowlist the agent enforces"* — it is not:
the agent additionally does a resolve-and-compare that the API cannot replicate.

---

### SEV-3-2 — Agent returns HTTP 500 (`RecursionError`) on deeply nested JSON; the API does not

The API installs a custom `RequestValidationError` handler (`api/main.py`) that strips `input`
from each error — added to stop credential reflection. That handler also happens to be what
protects it here. The agent app registers no handler, so FastAPI's default one tries to
serialise the echoed `input` (the whole nested body) and blows the stack.

```bash
for d in 400 1000; do
  python3 -c "import sys;d=$d;sys.stdout.write('['*d+']'*d)" > n.json
  curl -o /dev/null -w "agent %{http_code}  " -X POST -H "Authorization: Bearer $T" \
       -H 'Content-Type: application/json' --data-binary @n.json http://127.0.0.1:8792/v1/stat
  curl -o /dev/null -w "api %{http_code}\n"  -X POST -H "Authorization: Bearer $T" \
       -H 'Content-Type: application/json' --data-binary @n.json http://127.0.0.1:8791/api/v1/block-list
done
# depth 400 :  agent 422   api 400
# depth 1000:  agent 500   api 400
```

Agent log: `RecursionError: maximum recursion depth exceeded`. Payload is 2000 bytes — well under
any cap. The agent survived (subsequent `/v1/health` returned 200), so this is a noisy 5xx and a
stack-exhaustion primitive rather than a crash. Secondary issue: the agent's default handler
**echoes the entire request body back** in the error, which for `/v1/epic/*` means reflecting
`raw_manifest_b64` — the same reflection the API deliberately removed.

---

### SEV-3-3 — `POST /api/v1/prefill-exclusions/{platform}/{app_id}` answers 503 for an over-long app_id instead of 400

The module defines `_AppId = Annotated[str, StringConstraints(min_length=1, max_length=64)]`
with the comment *"app_id length mirrors the table's CHECK … so a bad id is rejected as 400 at
the edge rather than surfacing a 503 CHECK failure"* — but applies it only to the reconcile
**body**. The `POST .../{platform}/{app_id}` **path parameter** is a bare `str`.

```bash
LONG=$(python3 -c 'print("A"*65)')
curl -s -w "\n%{http_code}\n" -X POST -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
     -d '{"mode":"exclude"}' "http://127.0.0.1:8791/api/v1/prefill-exclusions/steam/$LONG"
# {"detail":"database unavailable"}
# 503
```
API log: `{"reason": "check constraint failed", "event": "api.prefill_exclusions.write_failed"}`.
For contrast, `POST /api/v1/block-list` with the same 65-char `app_id` correctly returns 400
`string_too_long`.

**Impact.** Client error reported as a server outage: trips 5xx alerting, and makes a caller with
retry-on-503 hammer a request that can never succeed.

---

### SEV-3-4 — `_DETAIL_MAX` is not applied on the branch that handles almost every error

`cli/client.py::_error_detail` truncates to `_DETAIL_MAX` (200) on the structured-detail branch
and the raw-body branch, but the `isinstance(raw, str)` branch does `return raw.strip()` with no
bound — and that is the branch every normal `{"detail": "..."}` API error takes. The constant's
own docstring says it exists because *"an error body can be an arbitrarily large HTML page … and
this text lands on the operator's terminal"*, and the `#265` comment right below explains that
`resp.reason_phrase` was reverted specifically to keep ANSI escapes off that terminal.

```python
from orchestrator.cli.client import _error_detail, _DETAIL_MAX, OrchClient, ApiError
payload = "\x1b[2J\x1b]0;OWNED\x07" + "Z"*50000
r = httpx.Response(404, json={"detail": payload}, request=httpx.Request("GET","http://x/"))
_error_detail(r)                       # len 50014, contains ESC   <- not truncated
_error_detail(httpx.Response(404, json={"detail":[{"x":"Y"*50000}]}, ...))  # len 200  <- truncated
_error_detail(httpx.Response(404, content=b"Q"*50000, ...))                # len 200  <- truncated

# end-to-end through the client (MockTransport):
# ApiError message length = 50024, starts: 'HTTP 404: \x1b[2J\x1b]0;OWNED\x07ZZZZ…'
```

**Impact.** A wrong `--url` / `ORCH_API_URL`, a proxy in front of the API, or a MITM on the
CLI↔API hop can write unbounded attacker-chosen bytes — including terminal control sequences —
straight to the operator's stderr. See SEV-3-5 for what those sequences do.

---

### SEV-3-5 — `orchestrator-cli game show` writes game titles to the terminal verbatim, control characters included

Game titles are taken unfiltered from the Steam store API (`platform/steam/store.py`:
`name = data.get("name")`, type-checked only) and from Epic's catalog
(`platform/epic/library.py`: `str(title)`). `games.title` is `TEXT` with no CHECK.
`cli/commands/game.py::game_show` renders `f"{key:18} {value}"` through `click.echo`, which
strips CSI sequences **only when stdout is not a TTY** — the operator's normal case is a TTY.

```
# title = ESC[2J ESC[31m PWNED ESC[0m BEL ESC]0;hijacked BEL ESC[8m
$ orchestrator-cli game show 10        # run under a pty; bytes shown with cat -v
id                 10
platform           steam
app_id             9001
title              ^[[2J^[[31mPWNED^[[0m^G^[]0;hijacked^G^[[8m
owned              1
...
```

Every escape reaches the terminal: `ESC[2J` clears the scrollback above the record, `ESC]0;…BEL`
rewrites the window/tab title, and `ESC[8m` (conceal) makes **every following line invisible** —
so `status`, `last_error` and `blocked` are printed and unreadable. `\r` and `ESC[1A` in the same
field can overwrite an already-printed line with a different value.
(Piped output is partly protected by accident: click's `strip_ansi` regex covers CSI but not OSC,
so `ESC]0;hijacked BEL` and `BEL` survive even into a pipe.)
`output.table` also measures width with `len()`, so escapes corrupt `game list` column alignment.

**Impact.** Data from an external store rewrites what the operator sees in their own terminal —
including hiding the status field of the record they asked for. Exploiting it deliberately needs
influence over a store listing the operator owns, which is why this is SEV-3 and not higher; but
the same sink fires on any merely-corrupt title, and it is the only unescaped
remote-data → terminal path in the CLI.

---

### SEV-3-6 — CLI exit code 2 is overloaded: "you typed a bad id" is indistinguishable from "the API is down"

`cli/main.py` documents *"Exit codes (Manifesto F11): API unreachable -> 2, auth failure -> 3,
other -> 1"*, and `ApiUnreachableError.exit_code = 2`. But `click.BadParameter` (raised by
`_positive_int` / `_non_negative_int` / `click.Choice`) also exits 2.

```
$ orchestrator-cli game show 0            ; echo $?   # 2   (bad argument)
$ orchestrator-cli game list --offset -1  ; echo $?   # 2   (bad argument)
$ orchestrator-cli game list --status bogus; echo $?  # 2   (bad argument)
$ orchestrator-cli --url http://127.0.0.1:1 game show 1; echo $?  # 2  (API unreachable)
```

**Impact.** Any wrapper script or cron job that branches on exit 2 to mean "orchestrator is
down" (which is what the documented contract tells it to do) will misfire on a typo.

---

### SEV-3-7 — The anti-SSRF host guard admits internal FQDNs, contrary to its own docstring

`agent/routers/pull.py` and `platform/epic/manifest.py` share
`^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$`, documented as rejecting
*"an IP literal …, a bare/internal name, or a value carrying a port/path"*. It rejects only
**bare** (dotless) names.

```
metadata.google.internal  -> True      169.254.169.254 -> False
nas.lan                   -> True      localhost       -> False
router.local              -> True      10.0.0.1        -> False
intranet.corp             -> True      x.com:8080      -> False
127.0.0.1.nip.io          -> True      x.com/path      -> False
```

The value becomes the `Host:` header on a request the agent sends **through lancache**, and
lancache-monolithic routes an unmapped host by that header. `platform/epic/manifest.py` takes
`cdn_host` from Epic's own manifest response and states the guard exists so *"a hostile/MITM'd
response can't point the lancache at an arbitrary bare-hostname internal target"* — the guard
does not achieve that. `metadata.google.internal` and `127.0.0.1.nip.io` (which resolves to
127.0.0.1) both sail through.

**Impact.** SSRF-through-lancache to internal FQDNs, with the response cached. Reachability
requires a hostile/MITM'd Epic CDN response — i.e. exactly the threat the guard was written for.

---

### SEV-3-8 — `$`-anchored regexes accept a trailing newline; `GOG%0A` reaches a filesystem path

Python's `$` matches at end-of-string **or immediately before a trailing `\n`**. Every guard
regex here uses `$` with `re.match` and none uses `\Z`: `_LAUNCHER_RE` (both copies), `_HEX32`
(`agent/routers/stat.py`), `_HEX32_RE` / `_SHA_RE` (`validator/cache_key.py`), `_HOSTNAME_RE`
(pull + Epic manifest), `_STRICT_INT_RE`, `_IDENTIFIER_RE`, `_TIMESTAMP_RE`.

```bash
curl --path-as-is -H "Authorization: Bearer $T" http://127.0.0.1:8792/v1/manual-downloads/GOG%0A
# {"launcher":"GOG\n","present":false,"entries":[]}       <- accepted, used as a directory name
```
`"evil.com\n"` likewise passes `_HOSTNAME_RE`. No exploit today (h11 rejects the LF when the
header is written — see "Attacked and held"), but the guards are not enforcing what they claim,
and the same anchor bug will bite the next time one of these values reaches a sink that tolerates
control characters (a log line, a subprocess argv, a filename).

---

### SEV-3-9 — uvicorn's `proxy_headers` is on by default and *does* let a client header change an authorization decision

Confirmed mechanism, and confirmed **not** escalatable in the current deployment shape.

```bash
# From loopback, a client-supplied header flips the loopback-only gate:
curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
     http://127.0.0.1:8791/api/v1/openapi.json                                  # 200
curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" \
     -H "X-Forwarded-For: 10.0.0.5" http://127.0.0.1:8791/api/v1/openapi.json   # 403
```
`SourceAllowlistMiddleware` and the OQ2 loopback gate both read `scope["client"]`, and uvicorn's
`ProxyHeadersMiddleware` (enabled by default; `forwarded_allow_ips` defaults to `127.0.0.1`)
rewrites `scope["client"]` from `X-Forwarded-For` **before** they run. `Forwarded:` and
`X-Real-IP:` are ignored (200 in the same test).

The exploitable direction is self-demotion only. From a LAN peer nothing can be gained — see
"Attacked and held" for the full 0.0.0.0-bind test matrix.

**Why it still matters.** The `Dockerfile` ENTRYPOINT and the LXC runbook both invoke
`python -m uvicorn … --host … --port …` with **no `--no-proxy-headers`**. The moment
`FORWARDED_ALLOW_IPS` is set, a reverse proxy is introduced, or the container is published in a
way that makes the observed peer a fixed gateway address, the source-IP allowlist becomes fully
bypassable by a header. `_constants.py` already carries a DEPLOYMENT WARNING about the reverse-proxy
case; the one-flag mitigation is not applied.

---

### SEV-3-10 — Pagination integers use lenient `int()` while filter integers use a strict regex

Issue #87.P5 added `_STRICT_INT_RE = ^-?\d+$` to `_coerce_value` precisely because *"Python's
`int()` is lenient and accepts forms that no operator would actually type, blurring the wire
contract"*. `parse_pagination` was not given the same treatment, and neither was the FastAPI
path-param coercion.

```
?limit=1_0      -> limit 10      ?offset=1_0  -> offset 10
?limit=%2B5     -> limit 5       (i.e. "+5")
?limit=%D9%A5   -> limit 5       (Arabic-Indic ٥)
?limit=%EF%BC%91%EF%BC%90 -> limit 10   (fullwidth １０)
GET /api/v1/games/1_0  -> 404 (parsed as id 10)   GET /api/v1/games/+1 -> 200 (id 1)
GET /api/v1/games/01   -> 200 (id 1)
```
Cosmetic on its own; it means the documented wire contract is enforced in one of the two places
that parse integers, and `POST /api/v1/games/1_0/purge` is a destructive call reachable by a
spelling of the id the contract says is invalid.

---

## Attacked and held

Everything below was tried hard and resisted correctly. These are useful results.

**SQL / query-helper surface — 12,614 fuzzed GETs, zero 5xx.**
7 list endpoints × 34 parameter names × 53 hostile values (quotes, `; DROP TABLE games;--`,
`' OR 1=1--`, NUL, CRLF, RTL override, BOM, fullwidth digits, 5000-char strings, `1e309`,
`±2^63`, malformed ISO timestamps, `,,,`, `id:asc:desc`). Not one 5xx. Field names are
allow-listed before interpolation, values always bind through `?`, and both
`build_where_clause` and `build_order_by_clause` re-validate defensively.

**`contains` / LIKE (#264).** `%`, `_`, `\`, `\%`, `a\` are all escaped correctly and match
literally — `?title_contains=%` returned only the game whose title literally contains `%`, not
every row. The 200-char cap fires as a 400 both directly and through `game list --title`. No
wildcard-DoS pattern is constructible.

**`game_id` bounds (#263).** `9223372036854775807` → 404 (in range); `9223372036854775808`,
`-1`, `0`, `1e3`, `0x10`, `١`, `1%00` → 400. Consistent across `GET /games/{id}`,
`/prefill`, `/validate`, `/purge`. No overflow reaches aiosqlite.

**Purge path traversal — no route in.** Every purge path is derived from
`cache_path(root, md5hex, levels)`, and `cache_key` returns a 32-char md5 of
identifier+URI+range, so no attacker-controlled byte survives into a path segment.
`cache_path` re-validates the hex and asserts `is_relative_to(cache_root)`; `under_cache_root`
then drops anything whose `resolve()` is not strictly inside the root (rejecting the root
itself). Shared Steamworks redist depots 228981–228990 are excluded so a purge cannot delete
another game's chunks. `steam_purge` on an unknown app is `{"deleted":0}`, never an error.

**Purge / prefill dedup.** 60 concurrent `POST /api/v1/games/1/purge` → **one** job row
(`ON CONFLICT DO NOTHING` + `idx_jobs_purge_inflight`), 60 × HTTP 202 all carrying the same
`job_id`. The documented force-upgrade TOCTOU guard (`UPDATE … WHERE id=? AND state='queued'`)
is correct: it and `claim_next_job` are both `BEGIN IMMEDIATE` writes, so whichever lands first
wins and the other is a clean no-op. The worker is a single serial loop and the agent prefill is
post-then-poll, so a purge and a prefill for the same game cannot interleave through the
orchestrator.

**Auth / bearer.** Missing, wrong, oversized (8 KB vs the 4096 cap), non-ASCII, scheme-less,
and duplicate `Authorization` headers all 401. Case-insensitive `BEARER` and extra whitespace
correctly accepted (RFC 7235). `/api/v1/healthxxx` does **not** inherit `/api/v1/health`'s
exemption; `/api/v1/health/` does not either. `//api/v1/games`, `/api/v1/../v1/games`,
`/api/v1/health/../games`, `%2e%2e/`, and `/api/v1/games%2f` all 401 — no path-normalisation
bypass.

**Source-IP allowlist under a real off-loopback bind.** Bound to `0.0.0.0` with
`ORCH_ALLOWED_SOURCE_IPS=203.0.113.7`, from LAN peer `192.168.1.192`:

| request | result |
|---|---|
| loopback → `/api/v1/games` | 200 |
| LAN → `/api/v1/games` | 403 |
| LAN + `X-Forwarded-For: 127.0.0.1` | 403 |
| LAN + `X-Forwarded-For: 203.0.113.7` | 403 |
| LAN + `X-Real-IP` + `Forwarded: for=127.0.0.1` | 403 |
| LAN → `/api/v1/openapi.json` (loopback-only) | 403 |

**No escalation from a LAN peer by any header** — uvicorn will not read forwarded headers from an
untrusted peer, so the suspected weakness is refuted for this deployment shape (the residual
latent risk is SEV-3-9). The fail-closed boot guard also works: `_enforce_lan_bind_policy` /
`_enforce_agent_lan_bind_policy` `SystemExit(1)` on an off-loopback bind with an empty allowlist,
and `_is_source_allowed` returns False for `None`/unparseable clients (unix socket, no peer).

**Header injection into the chunk puller.** `_HOSTNAME_RE` admits `"evil.com\n"` (SEV-3-8), but
h11 refuses to serialise a header value containing LF:
`PullResult(chunks_failed=1, failures=[('/depot/1/chunk/aa', 'LocalProtocolError')])` — nothing
reaches the wire. `_validate_pull_url` correctly rejects `//evil.com/a`, `http://evil/a`,
`/a/../b`, `/a@b`, and the empty string.

**Manual-downloads traversal (#222).** `..`, `.`, `%2e%2e` → 400 at the agent
(`target.parent != root` after `resolve()`, so a symlinked launcher pointing outside also fails).
`..%2f..` → 404 (the route regex will not span `/`). Legitimate awkward names work end to end:
`Amazon Games` (space), `Itch.io` (dot), and `?include_files=true` correctly switches file
listing on; a non-boolean `include_files` is a clean 400. `AgentClient.manual_downloads`
percent-encodes with `quote(launcher, safe='')`, so nothing collapses on the way to the agent.

**Body-size cap on the API.** 42 KB → 413. This also (accidentally) makes
`GameshelfReconcileRequest.app_ids`' `max_length=50000` unreachable — a 32 KiB body holds at most
~5000 ids, comfortably under SQLite's 32766 bound-variable limit, so the `NOT IN (…)` reconcile
cannot be made to fail on variable count. A 348 KB push → 413.

**XSS into the status page.** All data-derived text goes through `escapeHtml` (`row()`) or
`textContent`. The only unescaped `innerHTML` interpolations (`setPill`) take literal strings and
integers. `applied_filters` echoes attacker text but the status page never renders it. Nothing
injectable found.

**Malformed-row and malformed-body handling.** Oversized (>64 KiB) and unparseable
`games.metadata` degrade to `null` with a structured warning, never a 500. Empty bodies, `{`,
`null`, `[]`, bare bytes, wrong content types, and unknown fields (`extra="forbid"`) all produce
clean 400s on the API. Huge request lines are rejected by h11 before reaching the app.

**CLI robustness.** No traceback escaped in any of ~16 adversarial invocations. `game show`
on out-of-range / non-integer ids, `--limit 0/99999`, `--offset -1`, `--status bogus`,
500-char `--title`, `--title "'; DROP TABLE games;--"` all produce clean messages. `#265`'s
bodiless-error handling is correct (no blank `HTTP 404:`), and `#264`'s `--offset` + truncation
footer work.

**Agent job store.** `AgentJobStore` evicts oldest-terminal at 1024 entries and never evicts a
running job — spamming `/v1/pull` does not grow it without bound.

---

## Notes

- **Purge is not reversible if the host prefill cron is mid-run.** `agent/routers/steam.py`'s
  `_prefill_gate` docstring says plainly that the gate covers agent-initiated runs only and that
  a host-cron SteamPrefill can overlap. Nothing serialises purge against a cron run. A purge that
  unlinks chunks while the cron's SteamPrefill believes it has already downloaded them leaves
  `successfullyDownloadedDepots.json` claiming success, so the next non-force prefill skips the
  app and the game stays empty until someone runs `--force`. That breaks the ADR-0015
  reversibility invariant. **Not reproduced** — it needs the real SteamPrefill binary — so it is
  recorded here rather than as a finding.
- **`has_more` can lie.** `games.py` computes `has_more = offset + len(games) < total`, where
  `len(games)` counts rows *after* `_row_to_game_response` drops unrenderable ones. If a row is
  ever dropped, `has_more` stays true while `shown` does not advance, and the `#264` CLI footer
  prints `use --offset <same value>` — an infinite page loop. I could not construct a droppable
  row (every `Literal` column is CHECK- or FK-constrained to exactly the allowed set), so this is
  latent, not confirmed. Counting fetched rows rather than rendered rows would close it.
- **Two 500s and three misleading 503s all trace to the same root cause**: hardening that was
  applied to `api/` was never applied to `agent/`. The agent shares `api/middleware.py` already;
  adding `BodySizeCapMiddleware` and the `RequestValidationError` handler to `create_agent_app`
  would close SEV-2-2 and SEV-3-2 together.
- Test artifacts (throwaway DB, cache roots, fuzz scripts, logs) are under the session scratchpad
  and were not written into the repo. Local API/agent processes were stopped at the end of the run.
