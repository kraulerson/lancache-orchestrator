# Known limitations

Operator-facing notes for behaviors that look like bugs but are deliberate constraints — typically from upstream library limitations we've chosen not to work around at current scope.

## Steam: container restart requires re-auth

**Symptom:** After `docker restart` or any container lifecycle event that wipes the worker subprocess's in-memory state, `GET /api/v1/platforms` shows `auth_status: "expired"` (or `"ok"` until the next operation surfaces NotAuthenticated). `/api/v1/platforms/steam/auth/status` returns `authenticated: false`. Subsequent library_sync / manifest_fetch jobs fail with `SteamWorkerError: NotAuthenticated`. The operator must re-auth via the standard two-step `POST /api/v1/platforms/steam/auth` + `POST /api/v1/platforms/steam/auth/{challenge_id}` flow including a fresh 2FA code from their authenticator app.

**Why it can't currently be fixed:** The orchestrator uses [steam-next](https://github.com/ValvePython/steam) 1.4.4 for both Steam network-protocol auth and CDN/manifest downloads. The library:

- Was last released 2023; GitHub master has no commits since 2023-05-05.
- Predates Steam's OAuth refresh-token rollout (late 2023/2024).
- Exposes only `SteamClient.login(username, password, login_key, auth_code, two_factor_code, login_id)` — no `refresh_token` parameter.
- Relies on the older `login_key` mechanism for password-free re-login, which Steam no longer issues for current accounts.

Spike A2 (`spikes/spike_a2_steam_modern.md`) details the API surface. Issue [#108](https://github.com/kraulerson/lancache-orchestrator/issues/108) tracks the closure; issue [#111](https://github.com/kraulerson/lancache-orchestrator/issues/111) is the open strategic tracker for revisiting (alternative library, OAuth implementation, etc.).

**Workaround:** Treat container restarts as auth events. After any restart:

```bash
# Re-auth (replace YOUR_USERNAME):
curl -s -i -H "Authorization: Bearer $ORCH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"YOUR_USERNAME","password":"YOUR_PASSWORD"}' \
  http://127.0.0.1:8765/api/v1/platforms/steam/auth
# Then submit 2FA code to the returned challenge_id endpoint.
```

**Practical impact:** Homelab deployments restart on the order of weeks (deploys, kernel updates) — the re-auth friction is bounded. For deployments that restart more frequently, consider declaring a Steam-spike-3 effort to track alternative libraries (see [#111](https://github.com/kraulerson/lancache-orchestrator/issues/111)).

**Detection:** Monitor `auth.auto_sync.queue_failed` warnings or `library_sync` jobs failing with `NotAuthenticated`. The `platforms.auth_status` row flips to `expired` automatically on the next library_sync attempt (F-UAT6-3 fix shipped in PR #110).
