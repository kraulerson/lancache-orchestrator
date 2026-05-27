# Spike A2 — steam-next 1.4.4 actual API (post-UAT-6)

**Date:** 2026-05-27
**Trigger:** UAT-6 live session revealed Spike A drift — the BL10/BL11 worker code referenced steam-next patterns that don't match the library's current API or modern Steam accounts.
**Method:** Read steam-next 1.4.4 source directly (installed locally into a temp venv).
**Scope:** Validate and re-design the worker layer for BL11 (library enumeration) and document a path forward (or lack of one) for session persistence on modern accounts.

## Findings

### F1 — `SteamClient.licenses` is `dict[int, License]`, populated asynchronously

**Source:** `steam/client/builtins/apps.py:14-23`.

```python
class Apps:
    licenses = None  # dict

    def __init__(self, *args, **kwargs):
        self.licenses = {}
        self.on(EMsg.ClientLicenseList, self._handle_licenses)

    def _handle_licenses(self, message):
        for entry in message.body.licenses:
            self.licenses[entry.package_id] = entry
```

- Keys are package_ids (ints). Values are `CMsgClientLicenseList.License` protobuf messages with `package_id`, `access_token`, plus many other fields (`time_created`, `flags`, `payment_method`, ...).
- Populated only when Steam sends `EMsg.ClientLicenseList`, which happens asynchronously after `SteamClient.login()` returns. The dict is **empty** in the microsecond after login.
- **Correct enumeration:** iterate `client.licenses.values()` (or just `.keys()` for the package_ids alone). The BL11 code iterated the dict itself, which yields the keys (ints), then called `getattr(int, "package_id")` returning None — populating no packages.
- **Correct wait:** `client.wait_event(EMsg.ClientLicenseList, timeout=N)` blocks (gevent-friendly) until the next license message arrives. **Subtle:** if the message ALREADY arrived before `wait_event` is called, the next call blocks indefinitely waiting for ANOTHER message. Polling the dict's truthiness with a deadline is safer.

### F2 — `get_product_info` does an extra round-trip when `auto_access_tokens=True`

**Source:** `steam/client/builtins/apps.py:44-104`.

```python
def get_product_info(self, apps=[], packages=[], ..., auto_access_tokens=True, timeout=15):
    if auto_access_tokens:
        tokens = self.get_access_tokens(app_ids=..., package_ids=...)
    ...
```

- Default behavior fetches access tokens for EVERY app and EVERY package before the actual product_info call.
- **Package tokens are already on `client.licenses[pid].access_token`** — fetching them again is wasted work.
- App access tokens aren't available pre-product-info, so the auto fetch IS needed for apps.
- **Optimization for packages:** pass `[{'packageid': pid, 'access_token': lic.access_token}]` and `auto_access_tokens=False` for the package call.

### F3 — `get_product_info(packages=N)` is bounded by Steam's CM job timeout

**Source:** `apps.py:44` default `timeout=15` (seconds). The call is implemented as a `send_job_and_wait` round trip with the CM server.

- A single call with hundreds of packages takes O(packages) on the server side. For a real Steam library (operator's case: presumably 500+ packages, 1000+ apps), the call exceeds the 15s default → returns None or times out.
- The orchestrator-side IPC timeout (30s default) sits ABOVE this; if `get_product_info` returns None internally, the IPC layer sees the worker just sat there. The IPC TimeoutError fires before the next response can be written.
- **Correct approach:** chunk packages and apps into batches of ~50. Each batch is its own `get_product_info` call. Collect results, return one combined response.

### F4 — Modern Steam accounts don't emit `EMsg.ClientNewLoginKey`

**Source:** `steam/client/__init__.py:50` (event name), `:68` (handler registration), `:224-235` (handler body).

```python
EVENT_NEW_LOGIN_KEY = 'new_login_key'
self.on(EMsg.ClientNewLoginKey, self._handle_login_key)

def _handle_login_key(self, message):
    self.login_key = message.body.login_key
    ok = self.store_sentry(self.username, message.body.bytes)
```

- The `login_key` mechanism (Steam's older "remember me" token, persistable for password-free re-login) is only emitted by Steam for accounts using a deprecated auth flow.
- **Operator's account (kraulerson, modern Steam): no `ClientNewLoginKey` ever fires.** Verified in UAT-6 live test — sentry dir empty, login_key attribute stays None, EVENT_NEW_LOGIN_KEY handler never invoked.
- `SteamClient.login()` accepts only: `username`, `password`, `login_key`, `auth_code`, `two_factor_code`, `login_id`. **No `refresh_token` parameter.** No way to feed in a modern-Steam OAuth refresh token.

### F5 — steam-next 1.4.4 is unmaintained at the latest version

**Source:** ValvePython/steam GitHub master branch — last commit 2023-05-05 ("ci: update action versions"). No newer versions on PyPI (1.4.4 is latest).

- Modern Steam moved to OAuth-style refresh tokens after steam-next's last release. The library predates the new auth flow.
- No alternative Python libraries that BOTH (a) do CDN/manifest downloads (required for BL12) AND (b) support modern auth. `steamio` is web-API only; `DepotDownloader` is C#.

## Implications for BL10 / BL11 / BL12

### Fixable now (this branch)

- **#107 — licenses dict iteration + arrival wait.** Iterate `.values()` (or use `.keys()` as package_ids since the dict is keyed by them), and wait for the dict to populate with a polling deadline.
- **#109 — `get_product_info` batching.** Chunk packages (~50 per call), chunk apps (~50 per call), pass package access tokens explicitly to skip the extra `get_access_tokens` round trip.

### NOT fixable without changing libraries

- **#108 — session persistence across container restart.** Modern Steam accounts (the operator's case, and presumably most current home users) cannot use steam-next 1.4.4's `login_key` mechanism because Steam doesn't emit the trigger event. The only persistent-state file steam-next writes (sentry) is also tied to the `ClientNewLoginKey` flow. **Operator must re-auth on every container restart for the foreseeable future.**

### Recommended close on #108

Close with detailed limitation note. File a long-running follow-up issue tagged `triage:strategic` covering Steam library evaluation — to re-open when (a) ValvePython/steam resumes maintenance with modern-auth support, OR (b) a viable alternative Python library emerges that handles both auth and CDN, OR (c) the project decides to implement the OAuth flow directly using `requests` + Steam's web endpoints (substantial effort — captchas, mobile guard, no upstream).

## Recommendations for BL12 (manifest fetcher)

BL12 was planned to consume `games.id` from the populated games table. With #107 + #109 fixed, the games table CAN populate (pending live UAT validation by operator). BL12 implementation can proceed once those two fixes ship AND are live-validated.

`manifest.fetch` will share the same `get_product_info(apps=...)` round-trip risk as BL11 — keep the batching utility shared between the two handlers. Document the per-handler IPC timeout policy: library_sync and manifest_fetch should both be on the high-timeout track (300s+), not the default 30s.

## Behaviors NOT investigated in this spike

- Live re-validation against a real account (operator-side only — Anthropic agent cannot drive credentialed Steam interaction).
- Whether refresh-token-based modern auth via `WebAuth` could be retrofitted to feed into `SteamClient` somehow (would require library forking / monkey-patching — high risk, deferred).
- Connection lifecycle nuances when the worker subprocess is gevent-patched and the orchestrator-side asyncio loop drives it via stdin pipes (BL10 tests passed; UAT-6 didn't see any IPC layer flakiness beyond the cap and timeout issues already filed).
