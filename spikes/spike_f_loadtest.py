"""Spike F — Load test: API responsiveness under sustained download pressure.

HARD GATE spike for ADR-0001 single-container monolith viability.

Proves:
  1. The API stays responsive (p99 /health < 100ms) while sustaining 32
     concurrent chunk downloads at >= 300 Mbps throughput for 10 minutes.
  2. /api/v1/games remains responsive (p99 < 500ms) under the same load.

Pass criteria:
  - Aggregate download throughput >= 300 Mbps sustained for full duration
  - GET /api/v1/health  p99 < 100ms
  - GET /api/v1/games   p99 < 500ms

Fail consequence:
  - Option B (subprocess-isolated downloader), ADR-0005 supersedes ADR-0001

Dependencies (install in spikes venv):
  pip install httpx

Usage:
  # Full test — download load + API polling (the real gate test)
  python spikes/spike_f_loadtest.py --mode full --lancache-host 192.168.1.40

  # Download only — verify Lancache throughput before the API exists
  python spikes/spike_f_loadtest.py --mode download-only --lancache-host 192.168.1.40

  # API only — verify API latency baseline with no download pressure
  python spikes/spike_f_loadtest.py --mode api-only --api-host localhost --api-port 8765

  # Quick smoke test (30s, 4 workers)
  python spikes/spike_f_loadtest.py --mode download-only --duration 30 --concurrency 4
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STEAM_VHOST = "lancache.steamcontent.com"
STEAM_USER_AGENT = "Valve/Steam HTTP Client 1.0"
DEFAULT_CHUNK_URI = "/depot/228981/chunk/652b6c9b4aa15a255b9cd513752dbb82169c9097"

# Targets
TARGET_THROUGHPUT_MBPS = 300.0
TARGET_HEALTH_P99_MS = 100.0
TARGET_GAMES_P99_MS = 500.0

STATS_INTERVAL_S = 10.0


# ---------------------------------------------------------------------------
# Percentile helper (no numpy)
# ---------------------------------------------------------------------------

def percentile(sorted_values: list[float], pct: float) -> float:
    """Return the pct-th percentile from a pre-sorted list (0-100 scale)."""
    if not sorted_values:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class DownloadStats:
    """Accumulator for download worker metrics."""

    total_bytes: int = 0
    total_requests: int = 0
    total_errors: int = 0
    # Per-request latencies (seconds) — only the last stats window
    window_latencies: list[float] = field(default_factory=list)
    # All latencies for final summary
    all_latencies: list[float] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, nbytes: int, latency: float) -> None:
        async with self.lock:
            self.total_bytes += nbytes
            self.total_requests += 1
            self.window_latencies.append(latency)
            self.all_latencies.append(latency)

    async def record_error(self) -> None:
        async with self.lock:
            self.total_errors += 1

    async def drain_window(self) -> list[float]:
        async with self.lock:
            lats = self.window_latencies
            self.window_latencies = []
            return lats


@dataclass
class APIStats:
    """Accumulator for a single API endpoint's latency metrics."""

    endpoint: str = ""
    total_requests: int = 0
    total_errors: int = 0
    window_latencies: list[float] = field(default_factory=list)
    all_latencies: list[float] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, latency: float) -> None:
        async with self.lock:
            self.total_requests += 1
            self.window_latencies.append(latency)
            self.all_latencies.append(latency)

    async def record_error(self) -> None:
        async with self.lock:
            self.total_errors += 1
            self.total_requests += 1

    async def drain_window(self) -> list[float]:
        async with self.lock:
            lats = self.window_latencies
            self.window_latencies = []
            return lats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Spike F: Load test — API responsiveness under download pressure",
    )
    p.add_argument(
        "--lancache-host",
        default=os.environ.get("LANCACHE_HOST", "lancache"),
        help="Lancache IP or hostname (default: $LANCACHE_HOST or 'lancache')",
    )
    p.add_argument(
        "--api-host",
        default="localhost",
        help="Orchestrator API host (default: localhost)",
    )
    p.add_argument(
        "--api-port",
        type=int,
        default=8765,
        help="Orchestrator API port (default: 8765)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=32,
        help="Number of concurrent download workers (default: 32)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=600,
        help="Test duration in seconds (default: 600 = 10 min)",
    )
    p.add_argument(
        "--health-interval",
        type=float,
        default=0.5,
        help="Seconds between /health polls (default: 0.5)",
    )
    p.add_argument(
        "--games-interval",
        type=float,
        default=2.0,
        help="Seconds between /games polls (default: 2.0)",
    )
    p.add_argument(
        "--chunk-uri",
        default=DEFAULT_CHUNK_URI,
        help=f"Chunk URI path to download (default: {DEFAULT_CHUNK_URI})",
    )
    p.add_argument(
        "--mode",
        choices=["download-only", "api-only", "full"],
        default="full",
        help="Test mode (default: full)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Download load generator
# ---------------------------------------------------------------------------

async def download_worker(
    worker_id: int,
    client: httpx.AsyncClient,
    chunk_url: str,
    semaphore: asyncio.Semaphore,
    stats: DownloadStats,
    stop_event: asyncio.Event,
) -> None:
    """Single download worker — loops until stop_event is set."""
    while not stop_event.is_set():
        async with semaphore:
            if stop_event.is_set():
                break
            try:
                t0 = time.monotonic()
                resp = await client.get(chunk_url)
                body = resp.content
                latency = time.monotonic() - t0
                if resp.status_code == 200:
                    await stats.record(len(body), latency)
                else:
                    print(
                        f"[WARN] Worker {worker_id}: HTTP {resp.status_code} "
                        f"for {chunk_url}",
                    )
                    await stats.record_error()
            except (httpx.HTTPError, OSError) as exc:
                print(f"[WARN] Worker {worker_id}: {type(exc).__name__}: {exc}")
                await stats.record_error()
                # Brief back-off on error to avoid tight error loops
                await asyncio.sleep(0.5)


async def run_download_load(
    args: argparse.Namespace,
    stats: DownloadStats,
    stop_event: asyncio.Event,
) -> None:
    """Spawn download workers and let them run until stop_event fires."""
    chunk_url = f"http://{args.lancache_host}{args.chunk_uri}"
    semaphore = asyncio.Semaphore(args.concurrency)

    headers = {
        "Host": STEAM_VHOST,
        "User-Agent": STEAM_USER_AGENT,
    }

    print(f"[INFO] Starting {args.concurrency} download workers -> {chunk_url}")

    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(
            max_connections=args.concurrency + 4,
            max_keepalive_connections=args.concurrency + 4,
        ),
        # Follow redirects in case Lancache issues any
        follow_redirects=True,
    ) as client:
        tasks = [
            asyncio.create_task(
                download_worker(i, client, chunk_url, semaphore, stats, stop_event),
                name=f"dl-worker-{i}",
            )
            for i in range(args.concurrency)
        ]
        # Wait for stop signal, then let workers drain
        await stop_event.wait()
        # Give workers a moment to finish in-flight requests
        await asyncio.sleep(0.5)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# API pollers
# ---------------------------------------------------------------------------

async def api_poller(
    endpoint: str,
    base_url: str,
    interval: float,
    stats: APIStats,
    stop_event: asyncio.Event,
) -> None:
    """Poll a single API endpoint at the given interval until stopped."""
    warned = False
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
    ) as client:
        while not stop_event.is_set():
            try:
                t0 = time.monotonic()
                resp = await client.get(f"{base_url}{endpoint}")
                latency = time.monotonic() - t0
                if resp.status_code == 200:
                    await stats.record(latency)
                    warned = False
                else:
                    print(
                        f"[WARN] API {endpoint}: HTTP {resp.status_code}",
                    )
                    await stats.record_error()
            except (httpx.HTTPError, OSError) as exc:
                if not warned:
                    print(
                        f"[WARN] API {endpoint} unreachable: "
                        f"{type(exc).__name__}: {exc} (will keep retrying)",
                    )
                    warned = True
                await stats.record_error()
            # Sleep for the interval, but check stop_event more frequently
            sleep_end = time.monotonic() + interval
            while time.monotonic() < sleep_end and not stop_event.is_set():
                await asyncio.sleep(min(0.1, sleep_end - time.monotonic()))


async def run_api_pollers(
    args: argparse.Namespace,
    health_stats: APIStats,
    games_stats: APIStats,
    stop_event: asyncio.Event,
) -> None:
    """Launch API poller tasks for /health and /games."""
    base_url = f"http://{args.api_host}:{args.api_port}"

    print(f"[INFO] Starting API pollers -> {base_url}")
    print(
        f"[INFO]   /api/v1/health every {args.health_interval}s, "
        f"/api/v1/games every {args.games_interval}s",
    )

    health_task = asyncio.create_task(
        api_poller(
            "/api/v1/health",
            base_url,
            args.health_interval,
            health_stats,
            stop_event,
        ),
        name="api-health",
    )
    games_task = asyncio.create_task(
        api_poller(
            "/api/v1/games",
            base_url,
            args.games_interval,
            games_stats,
            stop_event,
        ),
        name="api-games",
    )

    await stop_event.wait()
    await asyncio.sleep(0.2)
    health_task.cancel()
    games_task.cancel()
    await asyncio.gather(health_task, games_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Stats reporter
# ---------------------------------------------------------------------------

def _fmt_bytes(nbytes: int) -> str:
    """Human-readable byte count."""
    if nbytes < 1024:
        return f"{nbytes} B"
    for unit in ("KB", "MB", "GB", "TB"):
        nbytes_f = nbytes / 1024
        if nbytes_f < 1024 or unit == "TB":
            return f"{nbytes_f:.1f} {unit}"
        nbytes = int(nbytes_f)
    return f"{nbytes} B"


def _fmt_pcts(sorted_lats_ms: list[float]) -> str:
    """Format p50/p95/p99 from sorted ms latencies."""
    if not sorted_lats_ms:
        return "no data"
    p50 = percentile(sorted_lats_ms, 50)
    p95 = percentile(sorted_lats_ms, 95)
    p99 = percentile(sorted_lats_ms, 99)
    return f"p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms"


async def stats_reporter(
    start_time: float,
    duration: float,
    dl_stats: DownloadStats | None,
    health_stats: APIStats | None,
    games_stats: APIStats | None,
    stop_event: asyncio.Event,
) -> None:
    """Print a progress line every STATS_INTERVAL_S seconds."""
    while not stop_event.is_set():
        # Wait for the interval, but exit early if stopped
        sleep_end = time.monotonic() + STATS_INTERVAL_S
        while time.monotonic() < sleep_end and not stop_event.is_set():
            await asyncio.sleep(min(0.5, sleep_end - time.monotonic()))

        if stop_event.is_set():
            break

        elapsed = time.monotonic() - start_time
        remaining = max(0, duration - elapsed)

        parts: list[str] = [f"[INFO] t={elapsed:.0f}s ({remaining:.0f}s left)"]

        # Download throughput
        if dl_stats is not None:
            throughput_mbps = (dl_stats.total_bytes * 8) / (elapsed * 1_000_000)
            window_lats = await dl_stats.drain_window()
            window_lats_ms = sorted(lat * 1000 for lat in window_lats)
            avg_chunk_ms = (
                f"{percentile(window_lats_ms, 50):.0f}ms"
                if window_lats_ms
                else "n/a"
            )
            parts.append(
                f"DL: {throughput_mbps:.1f}Mbps "
                f"({_fmt_bytes(dl_stats.total_bytes)}, "
                f"{dl_stats.total_requests} reqs, "
                f"chunk~{avg_chunk_ms})"
            )

        # Health latency window
        if health_stats is not None:
            window_lats = await health_stats.drain_window()
            window_lats_ms = sorted(lat * 1000 for lat in window_lats)
            parts.append(f"/health: {_fmt_pcts(window_lats_ms)}")

        # Games latency window
        if games_stats is not None:
            window_lats = await games_stats.drain_window()
            window_lats_ms = sorted(lat * 1000 for lat in window_lats)
            parts.append(f"/games: {_fmt_pcts(window_lats_ms)}")

        print("  |  ".join(parts))


# ---------------------------------------------------------------------------
# Final summary and verdict
# ---------------------------------------------------------------------------

def print_summary(
    elapsed: float,
    mode: str,
    args: argparse.Namespace,
    dl_stats: DownloadStats | None,
    health_stats: APIStats | None,
    games_stats: APIStats | None,
) -> bool:
    """Print the final report and return True if PASS, False if FAIL."""
    sep = "=" * 64
    print()
    print(sep)
    print("SPIKE F — LOAD TEST RESULTS")
    print(sep)
    print(f"Duration:       {elapsed:.1f}s")
    print(f"Mode:           {mode}")

    verdicts: list[bool] = []

    # --- Download ---
    if dl_stats is not None:
        throughput_mbps = (
            (dl_stats.total_bytes * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0.0
        )
        all_lats_ms = sorted(lat * 1000 for lat in dl_stats.all_latencies)
        avg_chunk_ms = (
            percentile(all_lats_ms, 50) if all_lats_ms else 0.0
        )

        print()
        print("--- Download Load ---")
        print(f"Concurrency:    {args.concurrency}")
        print(f"Total bytes:    {_fmt_bytes(dl_stats.total_bytes)}")
        print(f"Throughput:     {throughput_mbps:.1f} Mbps (target: >={int(TARGET_THROUGHPUT_MBPS)})")
        print(f"Requests:       {dl_stats.total_requests}")
        print(f"Avg chunk time: {avg_chunk_ms:.0f} ms")
        print(f"Errors:         {dl_stats.total_errors}")

        dl_pass = throughput_mbps >= TARGET_THROUGHPUT_MBPS
        verdicts.append(dl_pass)

    # --- Health ---
    if health_stats is not None:
        all_lats_ms = sorted(lat * 1000 for lat in health_stats.all_latencies)
        p50 = percentile(all_lats_ms, 50)
        p95 = percentile(all_lats_ms, 95)
        p99 = percentile(all_lats_ms, 99)

        print()
        print("--- API Health (/api/v1/health) ---")
        print(f"Requests:       {health_stats.total_requests}")
        print(f"p50:            {p50:.0f} ms")
        print(f"p95:            {p95:.0f} ms")
        print(f"p99:            {p99:.0f} ms  (target: <{int(TARGET_HEALTH_P99_MS)})")
        print(f"Errors:         {health_stats.total_errors}")

        health_pass = p99 < TARGET_HEALTH_P99_MS if all_lats_ms else False
        verdicts.append(health_pass)

    # --- Games ---
    if games_stats is not None:
        all_lats_ms = sorted(lat * 1000 for lat in games_stats.all_latencies)
        p50 = percentile(all_lats_ms, 50)
        p95 = percentile(all_lats_ms, 95)
        p99 = percentile(all_lats_ms, 99)

        print()
        print("--- API Games (/api/v1/games) ---")
        print(f"Requests:       {games_stats.total_requests}")
        print(f"p50:            {p50:.0f} ms")
        print(f"p95:            {p95:.0f} ms")
        print(f"p99:            {p99:.0f} ms  (target: <{int(TARGET_GAMES_P99_MS)})")
        print(f"Errors:         {games_stats.total_errors}")

        games_pass = p99 < TARGET_GAMES_P99_MS if all_lats_ms else False
        verdicts.append(games_pass)

    # --- Verdict ---
    print()
    print("--- Verdict ---")

    if dl_stats is not None:
        throughput_mbps = (
            (dl_stats.total_bytes * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0.0
        )
        tag = "PASS" if throughput_mbps >= TARGET_THROUGHPUT_MBPS else "FAIL"
        print(
            f"Download throughput: {tag} "
            f"({throughput_mbps:.1f} Mbps vs >={int(TARGET_THROUGHPUT_MBPS)} target)"
        )

    if health_stats is not None:
        all_lats_ms = sorted(lat * 1000 for lat in health_stats.all_latencies)
        p99 = percentile(all_lats_ms, 99) if all_lats_ms else float("inf")
        tag = "PASS" if p99 < TARGET_HEALTH_P99_MS else "FAIL"
        print(
            f"Health p99:          {tag} "
            f"({p99:.0f} ms vs <{int(TARGET_HEALTH_P99_MS)}ms target)"
        )

    if games_stats is not None:
        all_lats_ms = sorted(lat * 1000 for lat in games_stats.all_latencies)
        p99 = percentile(all_lats_ms, 99) if all_lats_ms else float("inf")
        tag = "PASS" if p99 < TARGET_GAMES_P99_MS else "FAIL"
        print(
            f"Games p99:           {tag} "
            f"({p99:.0f} ms vs <{int(TARGET_GAMES_P99_MS)}ms target)"
        )

    overall = all(verdicts) if verdicts else False
    print()
    print(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    print(sep)

    if overall:
        print()
        print("[OK] ADR-0001 single-container monolith is viable.")
    else:
        print()
        print("[FAIL] ADR-0001 viability NOT confirmed.")
        print("[FAIL] Consider Option B: subprocess-isolated downloader (ADR-0005).")

    return overall


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def main() -> int:
    args = parse_args()

    print(f"[INFO] Spike F — Load test ({args.mode} mode)")
    print(f"[INFO] Duration: {args.duration}s, Concurrency: {args.concurrency}")

    if args.mode in ("download-only", "full"):
        print(f"[INFO] Lancache: {args.lancache_host}")
        print(f"[INFO] Chunk URI: {args.chunk_uri}")
    if args.mode in ("api-only", "full"):
        print(f"[INFO] API: http://{args.api_host}:{args.api_port}")
    print()

    stop_event = asyncio.Event()

    # Initialize stats collectors
    dl_stats: DownloadStats | None = None
    health_stats: APIStats | None = None
    games_stats: APIStats | None = None

    tasks: list[asyncio.Task] = []

    if args.mode in ("download-only", "full"):
        dl_stats = DownloadStats()
        tasks.append(
            asyncio.create_task(
                run_download_load(args, dl_stats, stop_event),
                name="download-load",
            ),
        )

    if args.mode in ("api-only", "full"):
        health_stats = APIStats(endpoint="/api/v1/health")
        games_stats = APIStats(endpoint="/api/v1/games")
        tasks.append(
            asyncio.create_task(
                run_api_pollers(args, health_stats, games_stats, stop_event),
                name="api-pollers",
            ),
        )

    # Stats reporter
    start_time = time.monotonic()
    reporter_task = asyncio.create_task(
        stats_reporter(
            start_time, args.duration,
            dl_stats, health_stats, games_stats,
            stop_event,
        ),
        name="stats-reporter",
    )

    # Duration timer
    print(f"[INFO] Test running for {args.duration}s — Ctrl+C to stop early")
    print()
    try:
        await asyncio.sleep(args.duration)
    except asyncio.CancelledError:
        pass

    elapsed = time.monotonic() - start_time
    print()
    print(f"[INFO] Duration reached ({elapsed:.1f}s). Stopping workers...")

    stop_event.set()

    # Wait for all tasks to wrap up
    reporter_task.cancel()
    await asyncio.gather(reporter_task, *tasks, return_exceptions=True)

    # Print summary and verdict
    passed = print_summary(
        elapsed, args.mode, args,
        dl_stats, health_stats, games_stats,
    )
    return 0 if passed else 1


def _run() -> int:
    """Entry point handling KeyboardInterrupt gracefully."""
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("[INFO] Interrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(_run())
