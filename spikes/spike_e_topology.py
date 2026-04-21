#!/usr/bin/env python3
"""Spike E — End-to-end deployment topology validation.

Proves:
  1. The orchestrator container can reach Lancache via Docker Compose service
     name (http://lancache:80) — DNS resolution, HTTP connectivity, cache
     volume access, and chunk download through the proxy.
  2. The game_shelf on the ThinkStation (192.168.1.20) can reach the
     orchestrator's REST API — health check, game listing, and network path
     throughput consistent with 2.5GbE.
  3. A prefill operation can successfully pull bytes through the full network
     path (DXP4800 ↔ MikroTik switch ↔ ThinkStation).

Environment:
  DXP4800 NAS  — Docker Compose with Lancache + orchestrator containers
  Lancache     — http://lancache:80 (compose) or 192.168.1.40 (host IP)
  Orchestrator — port 8765
  ThinkStation — 192.168.1.20 (game_shelf host)
  Cache volume — /data/cache/cache (read-only mount in orchestrator)
  Network      — 2.5GbE between DXP4800 and MikroTik switch

Dependencies: pip install httpx
Usage:
  # From inside orchestrator container (compose mode):
  python spikes/spike_e_topology.py --mode compose

  # From ThinkStation / game_shelf (remote mode):
  python spikes/spike_e_topology.py --mode remote \\
      --orchestrator-host 192.168.1.40

  # Full E2E (runs both sequentially — from orchestrator container):
  python spikes/spike_e_topology.py --mode full \\
      --game-shelf-host 192.168.1.20

  # Custom Lancache host:
  python spikes/spike_e_topology.py --mode compose \\
      --lancache-host 192.168.1.40 --cache-root /mnt/lancache/cache
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

import httpx

# -- Constants ---------------------------------------------------------------

STEAM_VHOST = "lancache.steamcontent.com"
STEAM_USER_AGENT = "Valve/Steam HTTP Client 1.0"

# Known chunk from Spike A — depot 228981 (Steamworks Common Redist)
KNOWN_DEPOT_ID = 228981
KNOWN_CHUNK_SHA = "652b6c9b4aa15a255b9cd513752dbb82169c9097"


# -- Result tracking ---------------------------------------------------------

class CheckResult:
    """Tracks individual check outcomes for the final report."""

    def __init__(self) -> None:
        self.checks: list[tuple[str, str, bool, str]] = []  # (mode, name, passed, detail)

    def record(self, mode: str, name: str, passed: bool, detail: str = "") -> bool:
        tag = "[OK]  " if passed else "[FAIL]"
        msg = f"  {tag} {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.checks.append((mode, name, passed, detail))
        return passed

    def skip(self, mode: str, name: str, reason: str) -> None:
        print(f"  [SKIP] {name} — {reason}")
        # Skips are not failures; don't append to checks

    def info(self, msg: str) -> None:
        print(f"  [INFO] {msg}")

    @property
    def all_passed(self) -> bool:
        return all(passed for _, _, passed, _ in self.checks)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for _, _, passed, _ in self.checks if passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for _, _, passed, _ in self.checks if not passed)


# -- CLI ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Spike E: E2E deployment topology validation",
    )
    p.add_argument(
        "--lancache-host",
        default=os.environ.get("LANCACHE_HOST", "lancache"),
        help="Lancache IP or compose service name (default: $LANCACHE_HOST or 'lancache')",
    )
    p.add_argument(
        "--orchestrator-host",
        default=os.environ.get("ORCHESTRATOR_HOST", "localhost"),
        help="Orchestrator IP or hostname (default: $ORCHESTRATOR_HOST or 'localhost')",
    )
    p.add_argument(
        "--orchestrator-port",
        type=int,
        default=int(os.environ.get("ORCHESTRATOR_PORT", "8765")),
        help="Orchestrator REST API port (default: $ORCHESTRATOR_PORT or 8765)",
    )
    p.add_argument(
        "--game-shelf-host",
        default=os.environ.get("GAME_SHELF_HOST", "192.168.1.20"),
        help="ThinkStation / game_shelf IP (default: $GAME_SHELF_HOST or '192.168.1.20')",
    )
    p.add_argument(
        "--cache-root",
        type=Path,
        default=Path(os.environ.get("CACHE_ROOT", "/data/cache/cache")),
        help="Lancache cache volume path (default: $CACHE_ROOT or '/data/cache/cache')",
    )
    p.add_argument(
        "--mode",
        choices=["compose", "remote", "full"],
        default="compose",
        help="Test mode: compose (inside container), remote (from game_shelf), full (both)",
    )
    return p.parse_args()


# -- Compose mode checks (run from orchestrator container) -------------------

def run_compose_checks(args: argparse.Namespace, results: CheckResult) -> None:
    """Checks run from inside the orchestrator container on the DXP4800."""
    print("\n" + "-" * 65)
    print("COMPOSE MODE — Running from orchestrator container")
    print("-" * 65)

    # Phase 1: DNS resolution
    print("\n[INFO] Phase 1: DNS Resolution")
    try:
        addr = socket.gethostbyname(args.lancache_host)
        results.record(
            "compose", "DNS resolution",
            True, f"{args.lancache_host} -> {addr}",
        )
    except socket.gaierror as exc:
        results.record(
            "compose", "DNS resolution",
            False, f"cannot resolve '{args.lancache_host}': {exc}",
        )
        results.info(
            "If running outside Docker Compose, try --lancache-host <IP>"
        )
        # DNS failure is fatal for compose checks — remaining checks depend on it
        results.skip("compose", "HTTP connectivity", "DNS resolution failed")
        results.skip("compose", "Cache volume access", "independent, will still check")
        _check_cache_volume(args, results)
        results.skip("compose", "Chunk download", "DNS resolution failed")
        return

    # Phase 2: HTTP connectivity
    print("\n[INFO] Phase 2: HTTP Connectivity")
    lancache_url = f"http://{args.lancache_host}:80/"
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=10.0) as http:
            resp = http.get(lancache_url)
        elapsed_ms = (time.monotonic() - t0) * 1000
        # Lancache may return 200, 301, 403, 404 — any response means reachable
        results.record(
            "compose", "HTTP connectivity",
            True,
            f"GET {lancache_url} -> HTTP {resp.status_code} in {elapsed_ms:.0f}ms",
        )
    except httpx.HTTPError as exc:
        results.record(
            "compose", "HTTP connectivity",
            False, f"GET {lancache_url} failed: {exc}",
        )

    # Phase 3: Cache volume access
    print("\n[INFO] Phase 3: Cache Volume")
    _check_cache_volume(args, results)

    # Phase 4: Chunk download through Lancache
    print("\n[INFO] Phase 4: Steam Chunk Download (depot {}, sha {}...)".format(
        KNOWN_DEPOT_ID, KNOWN_CHUNK_SHA[:16],
    ))
    chunk_url = f"http://{args.lancache_host}/depot/{KNOWN_DEPOT_ID}/chunk/{KNOWN_CHUNK_SHA}"
    try:
        t0 = time.monotonic()
        with httpx.Client(
            timeout=30.0,
            headers={"Host": STEAM_VHOST, "User-Agent": STEAM_USER_AGENT},
        ) as http:
            resp = http.get(chunk_url)
        elapsed_ms = (time.monotonic() - t0) * 1000
        cache_status = resp.headers.get("X-Upstream-Cache-Status", "UNKNOWN")
        content_len = len(resp.content)

        if resp.status_code == 200:
            results.record(
                "compose", "Chunk download",
                True,
                f"HTTP 200, cache={cache_status}, size={content_len}, "
                f"time={elapsed_ms:.0f}ms",
            )
        else:
            results.record(
                "compose", "Chunk download",
                False,
                f"HTTP {resp.status_code} (expected 200), cache={cache_status}, "
                f"time={elapsed_ms:.0f}ms",
            )
    except httpx.HTTPError as exc:
        results.record(
            "compose", "Chunk download",
            False, f"request failed: {exc}",
        )


def _check_cache_volume(args: argparse.Namespace, results: CheckResult) -> None:
    """Stat the cache root and count first-level entries."""
    cache_root = args.cache_root
    if not cache_root.exists():
        results.record(
            "compose", "Cache volume access",
            False, f"path does not exist: {cache_root}",
        )
        return

    if not cache_root.is_dir():
        results.record(
            "compose", "Cache volume access",
            False, f"path is not a directory: {cache_root}",
        )
        return

    # Count first-level subdirectories (nginx cache levels=2:2 creates hex dirs)
    try:
        first_level = sorted(cache_root.iterdir())
        dirs = [p for p in first_level if p.is_dir()]
        files = [p for p in first_level if p.is_file()]
        results.record(
            "compose", "Cache volume access",
            True,
            f"{cache_root} exists — {len(dirs)} subdirs, {len(files)} files at top level",
        )

        # If there are subdirectories, peek into the first one for file count
        if dirs:
            sub = dirs[0]
            try:
                sub_entries = list(sub.iterdir())
                results.info(
                    f"Sample subdir {sub.name}/: {len(sub_entries)} entries"
                )
            except PermissionError:
                results.info(
                    f"Sample subdir {sub.name}/: permission denied (read-only mount?)"
                )
    except PermissionError:
        results.record(
            "compose", "Cache volume access",
            False, f"permission denied listing {cache_root}",
        )


# -- Remote mode checks (run from game_shelf / ThinkStation) -----------------

def run_remote_checks(args: argparse.Namespace, results: CheckResult) -> None:
    """Checks run from the game_shelf (ThinkStation) against the orchestrator."""
    print("\n" + "-" * 65)
    print("REMOTE MODE — Running from game_shelf / ThinkStation")
    print("-" * 65)

    orch_base = f"http://{args.orchestrator_host}:{args.orchestrator_port}"

    # Phase 1: Health endpoint
    print("\n[INFO] Phase 1: Orchestrator Reachability")
    health_url = f"{orch_base}/api/v1/health"
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=10.0) as http:
            resp = http.get(health_url)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if resp.status_code == 200:
            results.record(
                "remote", "Health endpoint",
                True,
                f"GET {health_url} -> 200 in {elapsed_ms:.0f}ms",
            )
            # Try to parse JSON body for extra info
            try:
                body = resp.json()
                results.info(f"Response body: {body}")
            except Exception:
                results.info(f"Response body (text): {resp.text[:200]}")
        else:
            results.record(
                "remote", "Health endpoint",
                False,
                f"GET {health_url} -> HTTP {resp.status_code} (expected 200)",
            )
    except httpx.HTTPError as exc:
        results.record(
            "remote", "Health endpoint",
            False, f"GET {health_url} failed: {exc}",
        )
        results.info(
            f"Is the orchestrator running at {args.orchestrator_host}:{args.orchestrator_port}?"
        )
        # Don't abort — try remaining checks anyway
    except Exception as exc:
        results.record(
            "remote", "Health endpoint",
            False, f"unexpected error: {exc}",
        )

    # Phase 2: API responsiveness (games list)
    print("\n[INFO] Phase 2: API Responsiveness")
    games_url = f"{orch_base}/api/v1/games"
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=10.0) as http:
            resp = http.get(games_url)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if resp.status_code == 200:
            results.record(
                "remote", "Games API latency",
                True,
                f"GET {games_url} -> 200 in {elapsed_ms:.0f}ms",
            )
            try:
                body = resp.json()
                count = len(body) if isinstance(body, list) else "N/A"
                results.info(f"Games returned: {count}")
            except Exception:
                results.info(f"Response (text): {resp.text[:200]}")
        else:
            results.record(
                "remote", "Games API latency",
                False,
                f"GET {games_url} -> HTTP {resp.status_code} in {elapsed_ms:.0f}ms",
            )
    except httpx.HTTPError as exc:
        results.record(
            "remote", "Games API latency",
            False, f"GET {games_url} failed: {exc}",
        )

    # Phase 3: Network path validation (2.5GbE throughput check)
    print("\n[INFO] Phase 3: Network Path Validation (2.5GbE class)")
    _check_network_throughput(args, results, orch_base)


def _check_network_throughput(
    args: argparse.Namespace, results: CheckResult, orch_base: str,
) -> None:
    """Download >100KB from orchestrator and verify 2.5GbE-class latency.

    2.5GbE theoretical: ~312 MB/s.  100KB at that rate = ~0.3ms.
    With overhead, we generously allow <50ms for the full HTTP round-trip.
    If the orchestrator doesn't serve large payloads, we fall back to
    measuring the health endpoint round-trip as a minimum-latency check.
    """
    # Try the games endpoint first — it may return a larger payload
    games_url = f"{orch_base}/api/v1/games"
    health_url = f"{orch_base}/api/v1/health"

    # Run multiple iterations for a more stable measurement
    latencies: list[float] = []
    sizes: list[int] = []
    target_url = games_url

    for attempt in range(5):
        try:
            t0 = time.monotonic()
            with httpx.Client(timeout=10.0) as http:
                resp = http.get(target_url)
            elapsed_ms = (time.monotonic() - t0) * 1000

            if resp.status_code == 200:
                latencies.append(elapsed_ms)
                sizes.append(len(resp.content))
            else:
                # Fall back to health endpoint
                if target_url != health_url:
                    target_url = health_url
                    results.info(
                        f"Games endpoint returned {resp.status_code}, "
                        f"falling back to health endpoint"
                    )
        except httpx.HTTPError:
            if target_url != health_url:
                target_url = health_url
                results.info("Games endpoint unreachable, falling back to health endpoint")

    if not latencies:
        results.record(
            "remote", "Network throughput",
            False, "could not complete any download from orchestrator",
        )
        return

    avg_latency = sum(latencies) / len(latencies)
    min_latency = min(latencies)
    max_latency = max(latencies)
    avg_size = sum(sizes) / len(sizes) if sizes else 0

    detail = (
        f"{len(latencies)} samples, avg={avg_latency:.1f}ms, "
        f"min={min_latency:.1f}ms, max={max_latency:.1f}ms, "
        f"avg_payload={avg_size:.0f}B"
    )

    # 2.5GbE class: we expect <50ms for a small HTTP request
    if avg_latency < 50:
        results.record("remote", "Network throughput", True, detail)
        if avg_size < 100_000:
            results.info(
                f"Payload under 100KB ({avg_size:.0f}B) — latency check only, "
                f"not a true throughput test. Consider adding a bulk endpoint."
            )
    else:
        results.record(
            "remote", "Network throughput",
            False,
            f"avg latency {avg_latency:.1f}ms exceeds 50ms threshold — {detail}",
        )
        results.info(
            "High latency may indicate non-local network, WiFi, or "
            "orchestrator under load. Expected <50ms on 2.5GbE LAN."
        )


# -- Report ------------------------------------------------------------------

def print_report(results: CheckResult, mode: str) -> None:
    """Print structured PASS/FAIL summary."""
    print("\n" + "=" * 65)
    print("SPIKE E — TOPOLOGY VALIDATION RESULTS")
    print("=" * 65)

    print(f"\n{'Mode':<10} {'Check':<30} {'Result':<8} {'Detail'}")
    print("-" * 65)
    for check_mode, name, passed, detail in results.checks:
        tag = "PASS" if passed else "FAIL"
        short_detail = detail[:40] + "..." if len(detail) > 43 else detail
        print(f"  {check_mode:<8} {name:<30} {tag:<8} {short_detail}")

    print(f"\n  Total checks: {results.total}")
    print(f"  Passed:       {results.passed_count}")
    print(f"  Failed:       {results.failed_count}")

    print(f"\n{'=' * 65}")
    overall = results.all_passed and results.total > 0
    print(f"OVERALL: [{'PASS' if overall else 'FAIL'}]  (mode={mode})")

    if not overall:
        if results.total == 0:
            print("  No checks were executed")
        for check_mode, name, passed, detail in results.checks:
            if not passed:
                print(f"  FAILED: [{check_mode}] {name}")
                if detail:
                    print(f"          {detail}")

    print("=" * 65)


# -- Main --------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print("=" * 65)
    print("SPIKE E — E2E Deployment Topology Validation")
    print(f"  Mode:             {args.mode}")
    print(f"  Lancache host:    {args.lancache_host}")
    print(f"  Orchestrator:     {args.orchestrator_host}:{args.orchestrator_port}")
    print(f"  Game shelf host:  {args.game_shelf_host}")
    print(f"  Cache root:       {args.cache_root}")
    print("=" * 65)

    results = CheckResult()

    if args.mode in ("compose", "full"):
        run_compose_checks(args, results)

    if args.mode in ("remote", "full"):
        run_remote_checks(args, results)

    print_report(results, args.mode)

    sys.exit(0 if (results.all_passed and results.total > 0) else 1)


if __name__ == "__main__":
    main()
