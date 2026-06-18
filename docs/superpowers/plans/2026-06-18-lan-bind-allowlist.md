# Orchestrator LAN-bind + Source-IP Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the orchestrator API be bound to the LAN for Game_shelf while a fail-closed, application-level source-IP allowlist guarantees only declared sources can connect — and an off-loopback bind without an allowlist refuses to start.

**Architecture:** The bind itself is already env-driven (`ORCH_API_HOST` → uvicorn `--host`). This plan adds (1) an `allowed_source_ips` setting, (2) a dedicated `SourceAllowlistMiddleware` that 403s non-allowlisted sources on every path but is a pure no-op when the allowlist is empty, and (3) a fail-closed boot guard. OQ2's existing loopback-only gate (credential/2FA/schema endpoints) is untouched and keeps those endpoints unreachable from the LAN automatically.

**Tech Stack:** Python 3.12, FastAPI, pure-ASGI middleware, pydantic-settings v2, pytest + pytest-asyncio + httpx ASGITransport. Spec: `docs/superpowers/specs/2026-06-18-lan-bind-allowlist-design.md` (committed 84e4c91).

---

## Context the engineer needs (read before starting)

- **Branch:** `feat/lan-bind-allowlist` (already checked out, off `main`).
- **Framework hooks are active** in this repo. Per the build loop, this plan uses **no per-task commits** — all tasks are implemented TDD-style, then a SINGLE `feat` commit at the end (Task 7) after presenting A/B/C commit-structure options. The framework process-checklist may gate the commit; follow its prompts.
- **enforce-context7:** editing `core/settings.py` introduces `pydantic_settings.NoDecode`. If the hook blocks the edit, run `resolve-library-id` for the exact package `pydantic-settings`, then `query-docs` "NoDecode comma-separated env list field" before retrying. (Other edited files import already-used libs.)
- **Run tests:** `python -m pytest <path> -q` from the repo root. The full suite is `python -m pytest -q`.
- **Existing security machinery (do NOT change):**
  - OQ2 loopback gate lives in `BearerAuthMiddleware.__call__` (`api/middleware.py:225`), reading `scope["client"][0]` against `LOOPBACK_HOSTS` (`api/dependencies.py:75` = `{"127.0.0.1","::1","::ffff:127.0.0.1"}`).
  - `settings.py` emits a `config.api_bound_non_loopback` **warning** (model-validator `_emit_config_warnings`, `settings.py:352`) — keep it; it coexists with the new boot guard. `Settings(api_host="0.0.0.0")` must remain constructible (policy is enforced at boot, not config-load).
- **Test fixtures (httpx ASGITransport) already exist** in `tests/api/conftest.py`:
  - `client` → default transport, client host `127.0.0.1` (loopback).
  - `loopback_client` → `client=("127.0.0.1", 12345)`.
  - `external_client` → `client=("192.168.1.100", 54321)` (non-loopback).
  - `unit_app` → `create_app()` with pool dep overridden, no lifespan; its settings have an **empty** allowlist (no env set), so the new middleware is a no-op there and the whole existing suite stays green.
- **Settings override pattern:** `get_settings()` is `@lru_cache`d; tests do `monkeypatch.setenv("ORCH_…", …)` then `get_settings.cache_clear()`. A valid token for constructing `Settings` directly is any 32-char string, e.g. `"t" * 32`.

## File Structure

- **Modify** `src/orchestrator/core/settings.py` — add `allowed_source_ips` field (NoDecode + comma-split before-validator + CIDR after-validator) and an `allowed_source_networks` property.
- **Modify** `src/orchestrator/api/middleware.py` — add `import ipaddress`, the pure `_is_source_allowed` helper, and `SourceAllowlistMiddleware`.
- **Modify** `src/orchestrator/api/main.py` — import + register `SourceAllowlistMiddleware`; add `_enforce_lan_bind_policy(settings)`; call it at the top of `_lifespan`; remove the old non-loopback warning block.
- **Create** `tests/api/test_source_allowlist.py` — pure-matcher + middleware + boot-guard tests.
- **Modify** `tests/core/test_settings.py` — `allowed_source_ips` parsing/validation tests.
- **Modify** `README.md` — env-table row + LAN-exposure run section; update the stale warning paragraph.
- **Modify** `CHANGELOG.md`, `FEATURES.md` — record the feature.

---

### Task 1: `allowed_source_ips` setting + parsing + validation

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_settings.py` (it already imports `Settings` and uses a `VALID_TOKEN` 32-char constant — reuse them; if `VALID_TOKEN` isn't in scope in your class, use `"t" * 32`):

```python
class TestAllowedSourceIps:
    def test_default_is_empty(self):
        s = Settings(orchestrator_token="t" * 32)
        assert s.allowed_source_ips == []
        assert s.allowed_source_networks == []

    def test_comma_separated_string_parses_to_list(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "t" * 32)
        monkeypatch.setenv("ORCH_ALLOWED_SOURCE_IPS", " 10.100.23.102 , 10.0.0.0/24 ")
        from orchestrator.core.settings import get_settings
        get_settings.cache_clear()
        s = get_settings()
        assert s.allowed_source_ips == ["10.100.23.102", "10.0.0.0/24"]
        get_settings.cache_clear()

    def test_list_input_passes_through(self):
        s = Settings(orchestrator_token="t" * 32, allowed_source_ips=["10.100.23.102"])
        assert s.allowed_source_ips == ["10.100.23.102"]

    def test_allowed_source_networks_parses_entries(self):
        s = Settings(orchestrator_token="t" * 32, allowed_source_ips=["10.100.23.102", "10.0.0.0/24"])
        import ipaddress
        nets = s.allowed_source_networks
        assert ipaddress.ip_address("10.100.23.102") in nets[0]
        assert ipaddress.ip_address("10.0.0.55") in nets[1]

    def test_invalid_cidr_rejected_at_construction(self):
        import pytest
        with pytest.raises(Exception):  # pydantic ValidationError
            Settings(orchestrator_token="t" * 32, allowed_source_ips=["10.0.0.0/99"])

    def test_invalid_ip_rejected_at_construction(self):
        import pytest
        with pytest.raises(Exception):
            Settings(orchestrator_token="t" * 32, allowed_source_ips=["not-an-ip"])

    def test_allow_any_entry_is_accepted(self):
        s = Settings(orchestrator_token="t" * 32, allowed_source_ips=["0.0.0.0/0"])
        import ipaddress
        assert ipaddress.ip_address("8.8.8.8") in s.allowed_source_networks[0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/core/test_settings.py::TestAllowedSourceIps -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'allowed_source_ips'`.

- [ ] **Step 3: Implement the field, validators, and property**

In `src/orchestrator/core/settings.py`:

1. Add imports near the top (with the other stdlib / typing imports):

```python
import ipaddress
from functools import cached_property
from typing import Annotated
```

(`from functools import lru_cache` is already present; add `cached_property` alongside or on its own line. `Any` is already imported; add `Annotated`.)

2. Import `NoDecode` from pydantic-settings — extend the existing import:

```python
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
```

3. Add the field next to `cors_origins` (around `settings.py:72`):

```python
    # Source-IP allowlist for LAN exposure. Empty => no extra sources beyond
    # loopback (and the SourceAllowlistMiddleware is a pure no-op). Comma-
    # separated IPs/CIDRs in env; NoDecode keeps pydantic-settings from trying
    # to JSON-decode the value before our before-validator splits it.
    allowed_source_ips: Annotated[list[str], NoDecode] = Field(default_factory=list)
```

4. Add the validators (place them near `_reject_empty_cors_origin`, ~`settings.py:220`):

```python
    @field_validator("allowed_source_ips", mode="before")
    @classmethod
    def _split_allowed_source_ips(cls, v: Any) -> Any:
        """Accept a comma-separated env string or a real list. A bare env
        string like '10.100.23.102,10.0.0.0/24' is split + trimmed; empty
        segments are dropped."""
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("allowed_source_ips", mode="after")
    @classmethod
    def _validate_allowed_source_ips(cls, v: list[str]) -> list[str]:
        """Each entry must parse as an IP network (a bare IP becomes /32 or
        /128). Fail fast at construction on a malformed entry."""
        for entry in v:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as e:
                raise ValueError(f"invalid allowed_source_ips entry {entry!r}: {e}") from e
        return v
```

5. Add the parsed-networks property (place it after the validators, as a normal method on the class):

```python
    @cached_property
    def allowed_source_networks(
        self,
    ) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Parsed allowlist networks, consumed by SourceAllowlistMiddleware.
        Entries are pre-validated by _validate_allowed_source_ips."""
        return [ipaddress.ip_network(e, strict=False) for e in self.allowed_source_ips]
```

> NOTE: pydantic v2 supports `functools.cached_property` on models (it is treated as a non-field attribute). If construction raises specifically about `cached_property` assignment, fall back to a plain `@property` with the same body — the parse is O(1–2 entries), so per-call cost is negligible.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/core/test_settings.py::TestAllowedSourceIps -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Confirm no settings regressions**

Run: `python -m pytest tests/core/test_settings.py -q`
Expected: PASS (existing + new). In particular `test_non_loopback_host_warning_fires` still passes (unchanged).

---

### Task 2: Pure `_is_source_allowed` matcher

**Files:**
- Modify: `src/orchestrator/api/middleware.py`
- Test: `tests/api/test_source_allowlist.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_source_allowlist.py`:

```python
import ipaddress

import pytest

from orchestrator.api.middleware import _is_source_allowed


def _nets(*entries):
    return [ipaddress.ip_network(e, strict=False) for e in entries]


class TestIsSourceAllowed:
    def test_loopback_always_allowed(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("127.0.0.1", nets) is True
        assert _is_source_allowed("::1", nets) is True
        assert _is_source_allowed("::ffff:127.0.0.1", nets) is True

    def test_exact_ip_match(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("10.100.23.102", nets) is True
        assert _is_source_allowed("10.100.23.103", nets) is False

    def test_cidr_range(self):
        nets = _nets("10.0.0.0/24")
        assert _is_source_allowed("10.0.0.55", nets) is True
        assert _is_source_allowed("10.0.1.1", nets) is False

    def test_ipv4_mapped_ipv6_matches_ipv4_entry(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("::ffff:10.100.23.102", nets) is True

    def test_allow_any(self):
        nets = _nets("0.0.0.0/0")
        assert _is_source_allowed("8.8.8.8", nets) is True

    def test_none_client_rejected(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed(None, nets) is False

    def test_unparseable_client_rejected(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("not-an-ip", nets) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestIsSourceAllowed -q`
Expected: FAIL — `ImportError: cannot import name '_is_source_allowed'`.

- [ ] **Step 3: Implement the helper**

In `src/orchestrator/api/middleware.py`:

1. Add `import ipaddress` to the imports (top of file, with the other stdlib imports like `hashlib`, `hmac`).

2. Add the helper near the top (after the imports / module constants, before the middleware classes):

```python
def _is_source_allowed(
    client_host: str | None,
    allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """True if client_host is a loopback form, or its IP is contained in any
    allowed network. None/unparseable -> False (fail closed). Callers invoke
    this only when allowed_networks is non-empty (the enforcement switch lives
    in SourceAllowlistMiddleware)."""
    if client_host in LOOPBACK_HOSTS:
        return True
    if client_host is None:
        return False
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    # Normalize IPv4-mapped IPv6 (::ffff:a.b.c.d) so it matches IPv4 entries.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in allowed_networks)
```

(`LOOPBACK_HOSTS` is already imported at `middleware.py:25`.)

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestIsSourceAllowed -q`
Expected: PASS (7 tests).

---

### Task 3: `SourceAllowlistMiddleware`

**Files:**
- Modify: `src/orchestrator/api/middleware.py`
- Test: `tests/api/test_source_allowlist.py`

- [ ] **Step 1: Write the failing tests (synthetic ASGI scope, driven directly)**

Append to `tests/api/test_source_allowlist.py`:

```python
import pytest

from orchestrator.api.middleware import SourceAllowlistMiddleware


async def _collect(messages, send_list):
    async def send(msg):
        send_list.append(msg)
    return send


def _make_scope(client):
    return {"type": "http", "method": "GET", "path": "/api/v1/games",
            "headers": [], "client": client}


class _Settings:
    """Minimal settings stand-in for the middleware's get_settings() call."""
    def __init__(self, networks):
        self.allowed_source_networks = networks


@pytest.mark.asyncio
class TestSourceAllowlistMiddleware:
    async def _run(self, monkeypatch, networks, client):
        import orchestrator.api.middleware as mw
        monkeypatch.setattr(mw, "get_settings", lambda: _Settings(networks))
        reached = {"v": False}

        async def downstream(scope, receive, send):
            reached["v"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await SourceAllowlistMiddleware(downstream)(_make_scope(client), receive, send)
        return reached["v"], sent

    async def test_empty_allowlist_is_noop_allows_any_source(self, monkeypatch):
        reached, sent = await self._run(monkeypatch, [], ("203.0.113.9", 5000))
        assert reached is True
        assert sent[0]["status"] == 200

    async def test_enforcing_rejects_unlisted_source_403(self, monkeypatch):
        import ipaddress
        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, ("203.0.113.9", 5000))
        assert reached is False
        assert sent[0]["status"] == 403

    async def test_enforcing_allows_listed_source(self, monkeypatch):
        import ipaddress
        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, ("10.100.23.102", 5000))
        assert reached is True
        assert sent[0]["status"] == 200

    async def test_enforcing_allows_loopback(self, monkeypatch):
        import ipaddress
        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, ("127.0.0.1", 5000))
        assert reached is True

    async def test_enforcing_none_client_rejected(self, monkeypatch):
        import ipaddress
        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, None)
        assert reached is False
        assert sent[0]["status"] == 403

    async def test_non_http_scope_passes_through(self, monkeypatch):
        import orchestrator.api.middleware as mw
        monkeypatch.setattr(mw, "get_settings", lambda: _Settings([]))
        reached = {"v": False}

        async def downstream(scope, receive, send):
            reached["v"] = True

        async def send(msg):
            pass

        async def receive():
            return {}

        await SourceAllowlistMiddleware(downstream)({"type": "lifespan"}, receive, send)
        assert reached["v"] is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestSourceAllowlistMiddleware -q`
Expected: FAIL — `ImportError: cannot import name 'SourceAllowlistMiddleware'`.

- [ ] **Step 3: Implement the middleware**

In `src/orchestrator/api/middleware.py`, add after `_is_source_allowed` (and after the `BearerAuthMiddleware` class is fine too — place it logically near the other middlewares; it needs `get_settings`, already imported at `middleware.py:29`):

```python
# ----------------------------------------------------------------------
# SourceAllowlistMiddleware — LAN-exposure source-IP gate
# ----------------------------------------------------------------------


class SourceAllowlistMiddleware:
    """Reject connections whose peer IP is not loopback and not in
    settings.allowed_source_networks. Pure no-op when the allowlist is empty
    (the boot guard guarantees an empty allowlist only coincides with a
    loopback-only bind). Reads scope["client"] directly — no X-Forwarded-For
    trust, since the app is bound without a reverse proxy."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        allowed = get_settings().allowed_source_networks
        if allowed:  # enforcement switch: empty list => passthrough
            client_info = scope.get("client")
            client_host = client_info[0] if client_info else None
            if not _is_source_allowed(client_host, allowed):
                _log.warning(
                    "api.source.rejected",
                    reason="source_not_allowed",
                    path=scope["path"],
                    client_host=client_host,
                )
                await self._send_403(send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_403(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"forbidden: source not allowed"}',
            }
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestSourceAllowlistMiddleware -q`
Expected: PASS (6 tests).

---

### Task 4: Register the middleware in the app stack

**Files:**
- Modify: `src/orchestrator/api/main.py`
- Test: `tests/api/test_source_allowlist.py`

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/api/test_source_allowlist.py`. These build an app whose settings carry a non-empty allowlist and drive it through the full middleware stack:

```python
import time

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def enforcing_app(populated_pool, monkeypatch):
    """App whose settings allow only 10.100.23.102 (+ loopback)."""
    from orchestrator.api.dependencies import get_pool_dep
    from orchestrator.api.main import create_app
    from orchestrator.core.settings import get_settings

    monkeypatch.setenv("ORCH_TOKEN", "t" * 32)
    monkeypatch.setenv("ORCH_ALLOWED_SOURCE_IPS", "10.100.23.102")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_pool_dep] = lambda: populated_pool
    app.state.boot_time = time.monotonic()
    app.state.git_sha = "test-sha-deadbeef"
    yield app
    get_settings.cache_clear()


def _client(app, host):
    transport = httpx.ASGITransport(app=app, client=(host, 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
class TestSourceAllowlistIntegration:
    async def test_unlisted_source_403_on_health(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 403
        assert r.json()["detail"] == "forbidden: source not allowed"

    async def test_unlisted_source_403_on_games(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/games", headers={"Authorization": "Bearer " + "t" * 32})
        assert r.status_code == 403

    async def test_listed_source_without_token_401(self, enforcing_app):
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.get("/api/v1/games")
        assert r.status_code == 401

    async def test_listed_source_with_token_200(self, enforcing_app):
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.get("/api/v1/games", headers={"Authorization": "Bearer " + "t" * 32})
        assert r.status_code == 200

    async def test_listed_nonloopback_still_blocked_from_oq2_auth(self, enforcing_app):
        # Passes the source gate but OQ2 still requires loopback for /auth.
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.post(
                "/api/v1/platforms/steam/auth",
                headers={"Authorization": "Bearer " + "t" * 32},
                json={},
            )
        assert r.status_code == 403

    async def test_rejected_source_still_gets_correlation_id(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 403
        assert "x-correlation-id" in {k.lower() for k in r.headers}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestSourceAllowlistIntegration -q`
Expected: FAIL — the middleware isn't in the stack yet, so the unlisted source is NOT 403'd (`test_unlisted_source_403_on_health` returns 200/503, and `test_rejected_source_still_gets_correlation_id` fails on the status assertion).

- [ ] **Step 3: Register the middleware**

In `src/orchestrator/api/main.py`:

1. Extend the middleware import (around `main.py:24`):

```python
from orchestrator.api.middleware import (
    BearerAuthMiddleware,
    BodySizeCapMiddleware,
    CorrelationIdMiddleware,
    SourceAllowlistMiddleware,
)
```

2. Add the registration between `BodySizeCapMiddleware` and `CorrelationIdMiddleware` (around `main.py:326-328`). `add_middleware` prepends, so this places SourceAllowlist just inside CorrelationId at request time:

```python
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(SourceAllowlistMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        ...
    )
```

Update the order comment above the block to read: `CORS → CorrelationId → SourceAllowlist → BodySizeCap → BearerAuth`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestSourceAllowlistIntegration -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Confirm the existing API suite stays green**

Run: `python -m pytest tests/api -q`
Expected: PASS. (unit_app has an empty allowlist → middleware no-op; `external_client`-based OQ2 tests still 403 via OQ2.)

---

### Task 5: Fail-closed boot guard

**Files:**
- Modify: `src/orchestrator/api/main.py`
- Test: `tests/api/test_source_allowlist.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_source_allowlist.py`:

```python
from orchestrator.api.main import _enforce_lan_bind_policy
from orchestrator.core.settings import Settings


class TestLanBindGuard:
    def test_loopback_bind_empty_allowlist_ok(self):
        s = Settings(orchestrator_token="t" * 32, api_host="127.0.0.1")
        _enforce_lan_bind_policy(s)  # no raise

    def test_non_loopback_bind_without_allowlist_systemexit(self):
        import pytest
        s = Settings(orchestrator_token="t" * 32, api_host="0.0.0.0")  # noqa: S104
        with pytest.raises(SystemExit):
            _enforce_lan_bind_policy(s)

    def test_non_loopback_bind_with_allowlist_ok(self):
        s = Settings(
            orchestrator_token="t" * 32,
            api_host="0.0.0.0",  # noqa: S104
            allowed_source_ips=["10.100.23.102"],
        )
        _enforce_lan_bind_policy(s)  # no raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestLanBindGuard -q`
Expected: FAIL — `ImportError: cannot import name '_enforce_lan_bind_policy'`.

- [ ] **Step 3: Add the guard function and call it; remove the old warning block**

In `src/orchestrator/api/main.py`:

1. Add the guard function near `_detect_non_loopback_bind` (around `main.py:90`):

```python
def _enforce_lan_bind_policy(settings: Settings) -> None:
    """Fail-closed LAN-bind guard (security priority #1). A non-loopback bind
    MUST declare ORCH_ALLOWED_SOURCE_IPS; otherwise refuse to start. A loopback
    bind is always fine. Called at the top of the lifespan, before migrations,
    so a misconfiguration fails fast."""
    log = structlog.get_logger()
    bind_signal = _detect_non_loopback_bind(settings.api_host)
    if bind_signal is None:
        return
    if not settings.allowed_source_ips:
        log.critical(
            "api.boot.lan_bind_without_allowlist",
            api_host=bind_signal,
            hint=(
                "Set ORCH_ALLOWED_SOURCE_IPS to the permitted source(s) before "
                "binding off-loopback. Refusing to start."
            ),
        )
        raise SystemExit(1)
    log.info(
        "api.boot.lan_bind_gated",
        api_host=bind_signal,
        allowed_source_ips=settings.allowed_source_ips,
        note="auth/2fa/schema remain loopback-only (OQ2)",
    )
```

(`Settings` is the type from `orchestrator.core.settings`; import it for the annotation if not already imported — add `from orchestrator.core.settings import Settings, get_settings` or extend the existing settings import. If a circular-import risk appears, annotate as `"Settings"` under `TYPE_CHECKING` and keep the runtime import inside the function.)

2. Call it at the very top of `_lifespan`, right after `settings = get_settings()` (around `main.py:95`):

```python
    settings = get_settings()
    log = structlog.get_logger()

    # 0. Fail-closed LAN-bind guard — before any startup work.
    _enforce_lan_bind_policy(settings)
```

3. **Remove** the now-redundant warning block (the `bind_signal = _detect_non_loopback_bind(settings.api_host)` + `log.warning("api.boot.non_loopback_bind_warning", ...)` block around `main.py:233-248`). The new guard supersedes it (it logs `lan_bind_gated` on the allowed path).

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/api/test_source_allowlist.py::TestLanBindGuard -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Confirm lifespan/boot tests still pass**

Run: `python -m pytest tests/api -q -k "lifespan or boot or main or middleware or source"`
Expected: PASS. If any existing test asserted the old `api.boot.non_loopback_bind_warning` event, update it to expect `api.boot.lan_bind_gated` (with an allowlist) or `api.boot.lan_bind_without_allowlist` + SystemExit (without). Search: `grep -rn "non_loopback_bind_warning" tests/`.

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `FEATURES.md`

- [ ] **Step 1: README — env table row**

In `README.md`, add a row under `ORCH_CORS_ORIGINS` in the env table (~line 41):

```markdown
| `ORCH_ALLOWED_SOURCE_IPS` | comma-separated IPs/CIDRs | `[]` | Source-IP allowlist for LAN exposure. **Required** (non-empty) when `ORCH_API_HOST` is non-loopback, or the app refuses to start. Loopback is always allowed. |
```

- [ ] **Step 2: README — LAN-exposure run section + fix the stale warning paragraph**

In `README.md`, replace the "Loopback restriction" paragraph (~line 138) so it reflects the new fail-closed behavior, and add a LAN-exposure subsection after the run commands (~line 134):

```markdown
**LAN exposure (e.g. for Game_shelf).** To reach the API from another host, bind off-loopback AND declare the allowed source(s):

​```bash
# Container binds all interfaces in its namespace; the host publish is scoped
# to the LAN NIC so the port is not exposed on other host interfaces.
docker run -e ORCH_API_HOST=0.0.0.0 \
           -e ORCH_ALLOWED_SOURCE_IPS=10.100.23.102 \
           -p 192.168.1.40:8765:8765 ...
​```

The `SourceAllowlistMiddleware` 403s any source that is not loopback or in `ORCH_ALLOWED_SOURCE_IPS`, on every path. A non-loopback bind with an empty allowlist **refuses to start** (`api.boot.lan_bind_without_allowlist`). As an outer layer, also restrict the port at the host firewall, e.g. nftables:

​```
# allow only Game_shelf to reach the orchestrator port; drop the rest
nft add rule inet filter input ip saddr 10.100.23.102 tcp dport 8765 accept
nft add rule inet filter input tcp dport 8765 drop
​```

**Loopback restriction (unchanged):** the OpenAPI schema and Swagger/ReDoc UIs, plus credential-intake (`POST /platforms/{name}/auth`) and 2FA-submit, remain loopback-only — an allowlisted-but-remote host still gets 403 on those. Note OQ2 reads `scope["client"]` directly, so do **not** place a reverse proxy in front of the app (it would make every client look like loopback). This design binds uvicorn directly, no proxy.
```

(The `​` zero-width marks above are just to escape the nested fences in this plan — in the actual README use plain triple-backtick fences.)

- [ ] **Step 3: CHANGELOG**

In `CHANGELOG.md`, under the current unreleased section, add entries:

```markdown
### Security
- Source-IP allowlist (`SourceAllowlistMiddleware`) gates all API paths to loopback + `ORCH_ALLOWED_SOURCE_IPS` when the API is bound off-loopback; defense-in-depth over the bearer token for LAN exposure.
- Fail-closed boot guard: a non-loopback bind with no `ORCH_ALLOWED_SOURCE_IPS` refuses to start.

### Added
- `ORCH_ALLOWED_SOURCE_IPS` setting (comma-separated IPs/CIDRs) + `Settings.allowed_source_networks`.

### Infrastructure
- Documented LAN-exposure deploy recipe (LAN-scoped docker publish + host nftables rule) in README.
```

- [ ] **Step 4: FEATURES**

In `FEATURES.md`, add a line recording the LAN-bind + source-IP allowlist capability (match the file's existing row/section format).

- [ ] **Step 5: Sanity-check docs build/links**

Run: `grep -n "ORCH_ALLOWED_SOURCE_IPS" README.md CHANGELOG.md`
Expected: matches in both files.

---

### Task 7: Full verification + single commit + push + PR

- [ ] **Step 1: Full test suite**

Run: `python -m pytest -q 2>&1 | tail -20`
Expected: all green (no new failures vs. `main`). Investigate any failure before proceeding.

- [ ] **Step 2: Lint / type gates (match repo CI)**

Run the repo's configured checks (e.g. `ruff check src tests` and any mypy/pyright config). Expected: clean. Fix anything introduced by this change (e.g. the `# noqa: S104` markers on intentional `0.0.0.0` test binds are already included).

- [ ] **Step 3: Present commit-structure options, then commit**

Bring A/B/C commit-structure options to the user and WAIT for an explicit pick before committing (a Stop-hook relay is NOT approval). Recommended default — single `feat` commit:

```bash
git add src/orchestrator/core/settings.py \
        src/orchestrator/api/middleware.py \
        src/orchestrator/api/main.py \
        tests/api/test_source_allowlist.py \
        tests/core/test_settings.py \
        README.md CHANGELOG.md FEATURES.md \
        docs/superpowers/plans/2026-06-18-lan-bind-allowlist.md
git commit -m "feat(api): LAN-bind source-IP allowlist + fail-closed bind guard

- ORCH_ALLOWED_SOURCE_IPS setting (comma-separated IPs/CIDRs) + parsed
  allowed_source_networks
- SourceAllowlistMiddleware: 403s non-loopback, non-allowlisted sources on
  every path; pure no-op when the allowlist is empty
- fail-closed boot guard: off-loopback bind without an allowlist refuses to
  start (api.boot.lan_bind_without_allowlist)
- OQ2 loopback-only endpoints unchanged; README/CHANGELOG/FEATURES updated

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: Push (Claude pushes; user merges)**

```bash
git push -u origin feat/lan-bind-allowlist
```

(The branch is already off `main`, so the branch-safety hook allows the push.)

- [ ] **Step 5: Open the PR (do NOT merge)**

```bash
gh pr create --title "feat(api): LAN-bind source-IP allowlist + fail-closed bind guard" --body "<summary: what/why, the two locked decisions (plaintext+allowlist, documented firewall), test counts, and the note that this unblocks live Game_shelf F14-F17 verification>"
```

- [ ] **Step 6: Report**

Report the PR URL. Note that this unblocks **live** Game_shelf F14–F17 verification (set `ORCH_API_URL=http://192.168.1.40:8765` on the Game_shelf backend once deployed) and that the host nftables rule is a manual sudo step on `192.168.1.40`.

---

## Self-Review

- **Spec coverage:** §4.1 setting → Task 1; §4.3 middleware + enforcement switch → Tasks 2–4; §4.4 boot guard → Task 5; §5 "stays safe" (OQ2 untouched, bearer untouched) → verified by Task 4 `test_listed_nonloopback_still_blocked_from_oq2_auth` + Task 4 Step 5; §6 deploy recipe → Task 6; §7 test matrix → Tasks 1–5 (every listed case mapped); §8 YAGNI (no TLS/proxy/XFF/reload) → nothing added beyond scope.
- **Placeholder scan:** none — every code/test step is complete. The only conditional is the cached_property fallback note (a real pydantic-v2 nuance), which still gives exact code.
- **Type/name consistency:** `allowed_source_ips` (list[str]) and `allowed_source_networks` (list of ip_network) are used identically in settings, the middleware, and tests; `_is_source_allowed(client_host, allowed_networks)` signature matches all call sites; `SourceAllowlistMiddleware` registration name matches the import; `_enforce_lan_bind_policy(settings)` matches its tests.
- **Stack order:** registration `BearerAuth, BodySizeCap, SourceAllowlist, CorrelationId, CORS` (prepend semantics) ⇒ request-time `CORS → CorrelationId → SourceAllowlist → BodySizeCap → BearerAuth`, consistent across Task 4 and the spec.
- **Suite safety:** empty-allowlist no-op (Task 3) + httpx ASGITransport default client = loopback ⇒ the existing suite is unaffected; the one place to double-check is any test asserting `non_loopback_bind_warning` (Task 5 Step 5 handles it).
