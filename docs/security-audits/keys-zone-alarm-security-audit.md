# Security audit — keys_zone alarm

**Date:** 2026-09-18
**Branch:** `design/keys-zone-alarm`
**Phase:** 2.4 (Build Loop `keys-zone-alarm`, step 4 of 6)
**Persona:** Senior Security Engineer — hunt vulnerabilities, describe the
concrete exploit, do not check boxes.

**Findings: 0 (SEV-1: 0, SEV-2: 0, SEV-3: 0, SEV-4: 0).**

## Scope

Source changes only. Documentation, `.claude/*.json` state and `tests/` excluded.

| file | state |
|---|---|
| `tools/cache_catcher/kuma.py` | new |
| `tools/cache_catcher/key_budget.py` | new |
| `tools/cache_catcher/key_budget_probe.py` | new |
| `tools/cache_catcher/fanotify_guard.py` | modified |

## Deployment context that bounds exploitability

This code runs as **root** inside the `cache-catcher` container on the NAS
(`192.168.1.30`), with `pid=host` and the lancache filesystem mounted. LAN-only,
no internet exposure. Python 3.11.2 stdlib only. It consumes three untrusted-ish
inputs: process names and command lines from `/proc`, file names from fanotify
events, and `/log/keybudget.env` (trusted config on disk).

## Automated

- **Semgrep** `p/owasp-top-ten` + `.semgrep/` custom rules, run with `--error`
  over `tools/cache_catcher/`: **clean**.
- **gitleaks** on every staged commit: **clean**. The push URLs are credentials
  and live only in `/log/keybudget.env` on the NAS, never in the repo.
- **ruff** (incl. flake8-bandit `S`): **clean** on the three new modules;
  `fanotify_guard.py` is excluded as a vendored operational artifact.
- One custom-rule hit, `no-urllib-on-main-loop`, adjudicated below.

## Manual review — candidates raised and why each was discarded

**1. Query-string / request injection in `kuma.push` — `kuma.py:52-53`.**
`urllib.parse.urlencode` uses `quote_plus`, percent-encoding everything outside
`[A-Za-z0-9_.-~]`. Verified empirically against a hostile payload containing
`&`, `#`, CRLF, `://`, `%00` and `../`: the resulting query carried exactly one
`&` (the built-in separator) and no `#`, CR, LF, `/` or `:`. An
attacker-influenced `msg` cannot add or override parameters, split the request,
inject a fragment, or alter the request target. `status` is a literal `"up"` or
`"down"` at every call site, and `key_budget.verdict` returns only those two.
**Not exploitable.**

**2. SSRF via the push URL.** Host and scheme come only from `KUMA_PUSH_*` in
`/log/keybudget.env` — trusted config. `msg` controls nothing beyond the encoded
query, i.e. path-only at most. **Not a finding.**

**3. Foreign-process data reaching Kuma — `fanotify_guard.py` `alert()`.** The
`mode000` subject interpolates `comm`, a process name from `/proc` and therefore
attacker-influenced. It reaches `kuma.push` as `msg`. Impact stops at the
rendered monitor message by (1); the message is not re-parsed, not
shell-interpolated and not written back anywhere. This is message spoofing, not
injection. **Not a finding.**

**4. `/proc/<pid>/environ` exposure — `key_budget_probe.nginx_index_size_mb`.**
The function reads the whole nginx environ, which can hold secrets. `environ` is
a function-local that never leaves the function; only a parsed `int` is returned.
Every sink in the new code was traced: `append_history` writes four numeric
fields (so no CSV formula injection either); `probe_loop`'s logging emits
`result.status` / `result.msg` — numeric summary strings built in
`key_budget.verdict` — and on failure `type(exc).__name__` only, never
`str(exc)`, so an exception carrying an environ fragment cannot leak it. No push
URL, environ value or SMTP credential reaches a log, the CSV or a heartbeat.
**Not exploitable.**

**5. Path traversal — `key_budget_probe.sample_cache`.** `idx` comes from
`rng.sample(range(65536), …)`, so `idx >> 8` and `idx & 0xFF` are each `0..255`
rendered `{:02x}` — two hex characters, no separator, no `.`. `root` is the
module constant. Traversal is arithmetically impossible. **Not exploitable.**

**6. `load_cfg` env-file parsing.** `line.split("=", 1)`; no shell, no `eval`, no
`os.environ` mutation. Values are consumed by `int()`/`float()` or passed as the
push URL. Mirrors the pre-existing `load_env()` for `/log/alert.env`. **Not a
finding.**

**7. Unlocked sharing of `_evict_ts`, `_purge_ts` and `_last_sent`** between the
two new daemon threads and the fanotify main loop. `liveness_loop` only ever
*reads* them, via `len()` on a `deque` and `dict.get` — both atomic under the
GIL. Worst outcome is a heartbeat quoting a count one event stale. No lost alert
and no bypassed latch: `COOLDOWN` (900 s) re-arms `_last_sent` well inside
`EVICT_LATCH_SEC` (3600 s). **No concrete security consequence.**

**8. Poisoning the ceiling with a process named `nginx`.** A host process named
`nginx` could mis-calibrate `nginx_rss_bytes` / `nginx_index_size_mb`. Requires
prior code execution on the NAS host and yields only monitor mis-calibration — no
unauthorized access, data exposure or privilege gain. **Below the bar.**

**9. Alarm suppression via `classify_actor`.** Putting `-m orchestrator.agent` in
a process cmdline downgrades an eviction to a commanded purge. **Pre-existing** —
`delete_actor.py` is unchanged on this branch; the new code only routes the same
existing classification to a monitor. Out of scope here.

**10. Excluded by policy.** `kuma.push` blocking up to `TIMEOUT_SEC = 10` on the
fanotify reader thread could in principle back up the kernel event queue — DoS,
excluded, and it only fires after an alert has already been sent, behind the
900 s cooldown.

## Adjudicated rule suppression

`no-urllib-on-main-loop` (`.semgrep/orchestrator-rules.yaml:18`) bans
`import urllib.request` outright, citing TM-015 and ADR-0001: blocking I/O
starving the orchestrator's asyncio loop. `kuma.py` has no event loop — it runs
in a daemon thread in a container with no asyncio and no pip packages, so
`httpx.AsyncClient` is not installable there, and the call blocks one dedicated
thread for at most `TIMEOUT_SEC`. The rule's premise does not hold.

Suppressed **at line level** with the rationale in the file, rather than adding a
`paths.exclude` for the directory (which is what `no-sync-sqlite` does for
`migrate.py`). Line-level keeps the rule armed for every future file in
`tools/cache_catcher/` and keeps the suppression visible to anyone reviewing the
import. Approved by the Orchestrator before it was applied.

## Security-relevant properties this change preserves

- **Monitoring never breaks the thing it monitors.** `kuma.push` swallows every
  exception; `probe_loop` wraps `run_once` and still pushes DOWN on failure. A
  guard that died because Kuma was unreachable would be worse than no guard.
- **Silence is never an acceptable outcome.** Every run pushes, up or down. A
  failed sample, an unreadable ceiling and an insufficient history all push DOWN
  or say so in words — unknown never reads as safe.
- **A commanded purge still moves no monitor** (#337), so an operator action
  cannot train the alert channel to be ignored.

## Operator obligation carried to deployment

`/log/keybudget.env` holds three push URLs, each of which is a complete
credential. It must be written directly on the NAS, owned by root, mode `600`,
exactly as `/log/alert.env` is (`-rw------- root root`). It is not version
controlled. No code path logs a push URL.
