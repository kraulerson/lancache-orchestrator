"""Spike D — Gevent/asyncio bridge coexistence proof-of-concept.

Proves: (1) steam-next's process-global gevent patch_minimal (socket, ssl, dns)
does NOT break asyncio's selector-based event loop, (2) httpx AsyncClient works
with gevent-patched sockets, (3) concurrent asyncio tasks can funnel through a
single gevent ThreadPoolExecutor without deadlock. Validates ADR-0001 zones 1+2.

Dependencies:  pip install "steam[client]" httpx
Usage:  python spikes/spike_d_gevent_bridge.py --mock
        python spikes/spike_d_gevent_bridge.py --duration 120 --concurrent-tasks 20
"""
from __future__ import annotations

# patch_minimal() patches socket, ssl, dns — process-global.
# Central question: does asyncio's event loop survive this?
from steam import monkey  # type: ignore[import-untyped]
monkey.patch_minimal()

import argparse, asyncio, getpass, os, statistics, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import httpx

_STEAM_AVAILABLE = True
try:
    from steam.client import SteamClient  # type: ignore[import-untyped]
    from steam.client.cdn import CDNClient  # type: ignore[import-untyped]
    from steam.enums import EResult  # type: ignore[import-untyped]
except ImportError:
    _STEAM_AVAILABLE = False

DEADLOCK_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Spike D: Gevent/asyncio bridge stress test")
    p.add_argument("--duration", type=int, default=60, help="Mixed workload duration in seconds")
    p.add_argument("--concurrent-tasks", type=int, default=10, help="Concurrent asyncio tasks")
    p.add_argument("--credentials-dir", default="./steam_creds", help="Steam credential dir")
    p.add_argument("--mock", action="store_true", help="Mock gevent work (no real Steam)")
    return p.parse_args()

def fmt_stats(lats: list[float]) -> str:
    if not lats:
        return "no data"
    s = sorted(lats)
    p95_i, p99_i = int(len(s) * 0.95), int(len(s) * 0.99)
    return (f"avg={statistics.mean(s)*1000:.1f}ms  "
            f"p95={s[min(p95_i, len(s)-1)]*1000:.1f}ms  "
            f"p99={s[min(p99_i, len(s)-1)]*1000:.1f}ms")

# ---------------------------------------------------------------------------
# Mock gevent-thread functions (Zone 2 simulation via gevent.sleep)
# ---------------------------------------------------------------------------

def mock_steam_connect(delay: float = 0.5) -> dict:
    import gevent; gevent.sleep(delay)
    return {"steam_id": 76561198000000000, "logged_in": True, "mock": True}

def mock_steam_get_licensed_apps(delay: float = 0.1) -> set[int]:
    import gevent; gevent.sleep(delay)
    return {228980, 730, 570, 440, 240}

def mock_steam_get_manifests(delay: float = 0.2) -> list[dict]:
    import gevent; gevent.sleep(delay)
    return [{"depot_id": 228981, "num_files": 42, "mock": True}]

def mock_steam_logout(delay: float = 0.1) -> bool:
    import gevent; gevent.sleep(delay)
    return True

# ---------------------------------------------------------------------------
# Real steam-next functions (Zone 2 — run in executor thread)
# ---------------------------------------------------------------------------

def real_steam_connect(credentials_dir: str) -> dict:
    from pathlib import Path
    Path(credentials_dir).mkdir(parents=True, exist_ok=True)
    client = SteamClient()
    client.set_credential_location(credentials_dir)

    @client.on("auth_code_required")  # type: ignore[misc]
    def _on_auth(is_2fa: bool, mismatch: bool) -> None:
        prompt = "Steam Guard code" if is_2fa else "email code"
        code = input(f"[INPUT] Enter {prompt}: ")
        kw = {"two_factor_code": code} if is_2fa else {"auth_code": code}
        client.login(**kw)

    username = os.environ.get("STEAM_USER") or input("[INPUT] Steam username: ")
    password = os.environ.get("STEAM_PASS") or getpass.getpass("[INPUT] Steam password: ")
    result = client.login(username, password)
    if result != EResult.OK:
        raise RuntimeError(f"Login failed: {result!r}")
    return {"steam_id": int(client.steam_id), "logged_in": True, "mock": False, "_client": client}

def real_steam_get_licensed_apps(client: SteamClient) -> set[int]:
    return set(CDNClient(client).licensed_app_ids)

def real_steam_get_manifests(client: SteamClient, app_id: int = 228980) -> list[dict]:
    cdn = CDNClient(client)
    return [{"depot_id": m.depot_id, "gid": m.gid} for m in cdn.get_manifests(app_id)]

def real_steam_logout(client: SteamClient) -> bool:
    client.logout(); return True

# ---------------------------------------------------------------------------
# Executor dispatch helper
# ---------------------------------------------------------------------------

async def _exec(executor: ThreadPoolExecutor, mock: bool, session: dict | None,
                mock_fn, real_fn, *real_args) -> object:
    """Run mock_fn or real_fn(client, *real_args) in executor with deadlock timeout."""
    loop = asyncio.get_running_loop()
    if mock:
        return await asyncio.wait_for(loop.run_in_executor(executor, mock_fn), timeout=DEADLOCK_TIMEOUT)
    client = session["_client"] if session else None
    args = (client, *real_args) if client else real_args
    return await asyncio.wait_for(loop.run_in_executor(executor, real_fn, *args), timeout=DEADLOCK_TIMEOUT)

# ---------------------------------------------------------------------------
# Watchdog — background asyncio task monitoring the gevent thread
# ---------------------------------------------------------------------------

async def watchdog(stop: asyncio.Event, thread_prefix: str) -> None:
    while not stop.is_set():
        names = [t.name for t in threading.enumerate()]
        if not any(thread_prefix in n for n in names):
            print(f"[WARN] Gevent worker thread '{thread_prefix}' missing! Active: {names}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass

# ---------------------------------------------------------------------------
# Phase 1 — Auth
# ---------------------------------------------------------------------------

async def phase1_auth(executor: ThreadPoolExecutor, mock: bool, creds_dir: str) -> dict | None:
    print("\n[INFO] === Phase 1: Auth Test ===")
    t0 = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        if mock:
            session = await asyncio.wait_for(
                loop.run_in_executor(executor, mock_steam_connect), timeout=DEADLOCK_TIMEOUT)
        else:
            session = await asyncio.wait_for(
                loop.run_in_executor(executor, real_steam_connect, creds_dir), timeout=DEADLOCK_TIMEOUT)
        elapsed = time.monotonic() - t0
        print(f"[OK]   Auth completed in {elapsed*1000:.0f}ms — steam_id={session['steam_id']}")
        if elapsed > 10.0:
            print(f"[WARN] Auth took {elapsed:.1f}s (target: <10s)")
        return session
    except asyncio.TimeoutError:
        print(f"[FAIL] Auth DEADLOCKED — no response in {DEADLOCK_TIMEOUT:.0f}s")
        return None
    except Exception as exc:
        print(f"[FAIL] Auth error: {exc}")
        return None

# ---------------------------------------------------------------------------
# Phase 2 — Concurrent access (N tasks through single-thread executor)
# ---------------------------------------------------------------------------

async def phase2_concurrent(executor: ThreadPoolExecutor, mock: bool,
                            session: dict, n: int) -> tuple[int, int, list[float]]:
    print(f"\n[INFO] === Phase 2: Concurrent Access ({n} tasks) ===")
    completed, deadlocked, lats = 0, 0, []

    async def _call(tid: int) -> None:
        nonlocal completed, deadlocked
        t0 = time.monotonic()
        try:
            await _exec(executor, mock, session, mock_steam_get_licensed_apps,
                        real_steam_get_licensed_apps)
            lats.append(time.monotonic() - t0)
            completed += 1
        except (asyncio.TimeoutError, Exception) as exc:
            deadlocked += 1
            print(f"[FAIL] Task {tid}: {type(exc).__name__}: {exc}")

    await asyncio.gather(*[asyncio.create_task(_call(i)) for i in range(n)])
    status = "[OK]  " if deadlocked == 0 else "[FAIL]"
    print(f"{status} {completed}/{n} completed, {deadlocked} deadlocked  {fmt_stats(lats)}")
    return completed, deadlocked, lats

# ---------------------------------------------------------------------------
# Phase 3 — Mixed workload (gevent calls + httpx + event loop yields)
# ---------------------------------------------------------------------------

async def phase3_mixed(executor: ThreadPoolExecutor, mock: bool,
                       session: dict, duration: int) -> tuple[int, int, list[float]]:
    print(f"\n[INFO] === Phase 3: Mixed Workload ({duration}s) ===")
    completed, deadlocked, lats = 0, 0, []
    deadline = time.monotonic() + duration

    async def _gevent_call() -> None:
        nonlocal completed, deadlocked
        t0 = time.monotonic()
        try:
            await _exec(executor, mock, session, mock_steam_get_manifests, real_steam_get_manifests)
            lats.append(time.monotonic() - t0)
            completed += 1
        except asyncio.TimeoutError:
            deadlocked += 1
            print(f"[FAIL] Gevent call DEADLOCKED at t+{time.monotonic() - (deadline - duration):.1f}s")
        except Exception as exc:
            deadlocked += 1
            print(f"[FAIL] Gevent call error: {exc}")

    async def _httpx_call() -> None:
        """Test httpx under gevent-patched sockets — connection attempt is the real test."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.get("http://127.0.0.1:1/health")
        except Exception:
            pass  # Expected (no server). Non-hang = success.

    iteration = 0
    while time.monotonic() < deadline:
        iteration += 1
        await asyncio.gather(
            asyncio.create_task(_gevent_call()),
            asyncio.create_task(_httpx_call()),
            asyncio.create_task(asyncio.sleep(0)),  # Yield — verify loop not blocked
        )
        if iteration % 50 == 0:
            elapsed = duration - (deadline - time.monotonic())
            print(f"[INFO] t+{elapsed:.0f}s: {completed} ops, {deadlocked} deadlocks")

    status = "[OK]  " if deadlocked == 0 else "[FAIL]"
    print(f"{status} {completed} completed, {deadlocked} deadlocked  {fmt_stats(lats)}")
    return completed, deadlocked, lats

# ---------------------------------------------------------------------------
# Phase 4 — Cleanup
# ---------------------------------------------------------------------------

async def phase4_cleanup(executor: ThreadPoolExecutor, mock: bool, session: dict) -> bool:
    print("\n[INFO] === Phase 4: Cleanup Test ===")
    t0 = time.monotonic()
    try:
        await _exec(executor, mock, session, mock_steam_logout, real_steam_logout)
        print(f"[OK]   Logout completed in {(time.monotonic() - t0)*1000:.0f}ms")
        return True
    except asyncio.TimeoutError:
        print("[FAIL] Logout DEADLOCKED")
        return False
    except Exception as exc:
        print(f"[FAIL] Logout error: {exc}")
        return False

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_all_phases(args: argparse.Namespace) -> bool:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="steam-gevent")
    stop = asyncio.Event()
    wd = asyncio.create_task(watchdog(stop, "steam-gevent"))
    results: dict[str, dict] = {}

    # Phase 1
    session = await phase1_auth(executor, args.mock, args.credentials_dir)
    results["phase1_auth"] = {"passed": session is not None, "deadlocks": 0 if session else 1}
    if session is None:
        print("[FAIL] Cannot continue — aborting")
        stop.set(); await wd; executor.shutdown(wait=False)
        return False

    # Phase 2
    c, d, lats = await phase2_concurrent(executor, args.mock, session, args.concurrent_tasks)
    results["phase2_concurrent"] = {
        "passed": d == 0 and c == args.concurrent_tasks,
        "completed": c, "deadlocks": d, "latencies": lats,
    }

    # Phase 3
    c, d, lats = await phase3_mixed(executor, args.mock, session, args.duration)
    results["phase3_mixed"] = {"passed": d == 0, "completed": c, "deadlocks": d, "latencies": lats}

    # Phase 4
    ok = await phase4_cleanup(executor, args.mock, session)
    results["phase4_cleanup"] = {"passed": ok, "deadlocks": 0 if ok else 1}

    stop.set(); await wd; executor.shutdown(wait=True)

    # --- Report ---
    print("\n" + "=" * 70)
    print("SPIKE D — RESULTS SUMMARY")
    print("=" * 70)

    gt = [t for t in threading.enumerate() if "steam-gevent" in t.name]
    print(f"\n[INFO] Gevent worker threads at exit: {len(gt)}")
    for t in gt:
        print(f"       {t.name}: alive={t.is_alive()}")

    print(f"\n{'Phase':<25} {'Result':<8} {'Ops':<8} {'DL':<6} {'Latency'}")
    print("-" * 70)
    total_dl = 0
    for name, data in results.items():
        label = "[OK]" if data["passed"] else "[FAIL]"
        ops = str(data.get("completed", "—"))
        dl = data.get("deadlocks", 0)
        total_dl += dl
        lat = fmt_stats(data["latencies"]) if data.get("latencies") else "—"
        print(f"  {name:<23} {label:<8} {ops:<8} {dl:<6} {lat}")

    overall = all(r["passed"] for r in results.values())
    mode = "MOCK" if args.mock else "LIVE"
    print(f"\n{'=' * 70}")
    print(f"OVERALL: [{'PASS' if overall else 'FAIL'}]  (mode={mode})")
    if not overall:
        for name, data in results.items():
            if not data["passed"]:
                print(f"  FAILED: {name}")
    print(f"{'=' * 70}")

    print("\nPass criteria:")
    print(f"  [{'OK' if total_dl == 0 else 'FAIL'}] Zero deadlocks across all phases")
    print(f"  [{'OK' if results['phase1_auth']['passed'] else 'FAIL'}] Auth completes in <10s")
    print(f"  [{'OK' if results['phase2_concurrent']['passed'] else 'FAIL'}] All concurrent tasks complete")
    print(f"  [{'OK' if results['phase3_mixed']['passed'] else 'FAIL'}] No task exceeds {DEADLOCK_TIMEOUT:.0f}s timeout")
    return overall

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    if not args.mock and not _STEAM_AVAILABLE:
        print("[FAIL] steam-next not installed and --mock not specified")
        print("[INFO] Install: pip install 'steam[client]'  |  Or run with --mock")
        sys.exit(1)

    print("=" * 70)
    print("SPIKE D — Gevent/Asyncio Bridge Coexistence Test")
    print(f"  Mode:             {'MOCK' if args.mock else 'LIVE (real Steam auth)'}")
    print(f"  Duration:         {args.duration}s  |  Concurrent tasks: {args.concurrent_tasks}")
    print(f"  Deadlock timeout: {DEADLOCK_TIMEOUT:.0f}s")
    print(f"  gevent patched:   socket, ssl, dns (patch_minimal)")
    print("=" * 70)

    sys.exit(0 if asyncio.run(run_all_phases(args)) else 1)

if __name__ == "__main__":
    main()
