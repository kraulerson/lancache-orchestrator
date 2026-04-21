"""Spike A — Steam authentication, manifest retrieval, and Lancache chunk download.

Proves that:
  1. steam-next can authenticate with Steam and retrieve depot manifests
  2. httpx can download chunks through a Lancache caching proxy using the
     correct Host header override and User-Agent for cache-key matching
  3. Second-pass downloads of the same chunks produce cache HITs

Dependencies (install in a venv, NOT in the project's main requirements):
  pip install "steam[client]" httpx

Usage:
  python spikes/spike_a_steam_prefill.py --app-id 228980 --lancache-host 10.0.0.50
  python spikes/spike_a_steam_prefill.py --app-id 228980 --max-chunks 3

Environment variables:
  LANCACHE_HOST — default Lancache IP/hostname (fallback: "lancache")
  STEAM_USER    — Steam username (prompted if unset)
  STEAM_PASS    — Steam password (prompted if unset; prefer interactive prompt)
"""
from __future__ import annotations

# steam-next requires gevent monkey-patching BEFORE other imports
from steam import monkey  # type: ignore[import-untyped]
monkey.patch_minimal()

import argparse
import asyncio
import getpass
import os
import sys
import time
from pathlib import Path

import httpx
from steam.client import SteamClient  # type: ignore[import-untyped]
from steam.client.cdn import CDNClient  # type: ignore[import-untyped]
from steam.enums import EResult  # type: ignore[import-untyped]

STEAM_VHOST = "lancache.steamcontent.com"
STEAM_USER_AGENT = "Valve/Steam HTTP Client 1.0"
CACHE_STATUS_HEADER = "X-Upstream-Cache-Status"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Spike A: Steam auth + Lancache chunk download proof-of-concept",
    )
    p.add_argument(
        "--lancache-host",
        default=os.environ.get("LANCACHE_HOST", "lancache"),
        help="Lancache IP or hostname (default: $LANCACHE_HOST or 'lancache')",
    )
    p.add_argument(
        "--app-id", type=int, required=True,
        help="Steam app ID to test (e.g. 228980 for Steamworks Common Redist)",
    )
    p.add_argument(
        "--max-chunks", type=int, default=5,
        help="Max chunks to download per pass (default: 5)",
    )
    p.add_argument(
        "--credentials-dir", default="./steam_creds",
        help="Directory for steam-next sentry/credential files (default: ./steam_creds)",
    )
    return p.parse_args()


def steam_login(credentials_dir: str) -> SteamClient:
    """Authenticate with Steam interactively and return the connected client."""
    cred_path = Path(credentials_dir)
    cred_path.mkdir(parents=True, exist_ok=True)

    client = SteamClient()
    client.set_credential_location(str(cred_path))

    username = os.environ.get("STEAM_USER") or input("[INPUT] Steam username: ")
    password = os.environ.get("STEAM_PASS") or getpass.getpass("[INPUT] Steam password: ")

    print(f"[INFO] Logging in as '{username}'...")
    result = client.login(username, password)

    if result in (EResult.AccountLoginDeniedNeedTwoFactor,
                  EResult.AccountLogonDenied):
        if result == EResult.AccountLoginDeniedNeedTwoFactor:
            code = input("[INPUT] Enter Steam Guard Mobile Authenticator code: ")
            result = client.login(username, password, two_factor_code=code)
        else:
            code = input("[INPUT] Enter code sent to your email: ")
            result = client.login(username, password, auth_code=code)

    if result != EResult.OK:
        print(f"[FAIL] Login failed: {result!r}")
        sys.exit(1)

    print(f"[OK]   Logged in — Steam ID: {client.steam_id}")
    try:
        print(f"[INFO] Account has {len(client.licenses)} license(s)")
    except Exception:
        print("[INFO] Could not enumerate licenses (non-fatal)")

    return client


def _patch_cdn_manifest_gid():
    """Monkey-patch steam-next CDNClient to handle dict manifest GIDs.

    steam-next 1.4.4: depot_info['manifests']['public'] returns a dict
    like {'gid': '...', 'size': '...'} but get_manifests() passes it
    raw to int(), which fails. This patches get_app_depot_info() to
    normalize dict GIDs to plain strings before get_manifests() sees them.
    """
    original = CDNClient.get_app_depot_info

    def patched_get_app_depot_info(self, app_id):
        depots = original(self, app_id)
        for depot_id, depot_info in depots.items():
            if not isinstance(depot_info, dict):
                continue
            manifests = depot_info.get("manifests")
            if not isinstance(manifests, dict):
                continue
            for branch, val in manifests.items():
                if isinstance(val, dict) and "gid" in val:
                    manifests[branch] = val["gid"]
        return depots

    CDNClient.get_app_depot_info = patched_get_app_depot_info


_patch_cdn_manifest_gid()


def get_chunks(
    client: SteamClient, app_id: int, max_chunks: int,
) -> tuple[int, list[dict]]:
    """Retrieve manifests for *app_id* and return (depot_id, chunk_list)."""
    print(f"\n[INFO] Creating CDNClient and fetching manifests for app {app_id}...")
    cdn = CDNClient(client)
    manifests = cdn.get_manifests(app_id)
    print(f"[OK]   Retrieved {len(manifests)} depot manifest(s)")

    if not manifests:
        print("[FAIL] No manifests returned — is the app ID correct and licensed?")
        sys.exit(1)

    # Use the first manifest that has files with chunks
    for manifest in manifests:
        depot_id = manifest.depot_id
        all_chunks = [
            chunk
            for f in manifest.iter_files()
            for chunk in f.chunks
        ]
        if all_chunks:
            break
    else:
        print("[FAIL] No chunks found in any depot manifest")
        sys.exit(1)

    print(f"[INFO] Depot {depot_id}: {len(all_chunks)} total chunk(s)")
    selected = all_chunks[:max_chunks]
    print(f"[INFO] Selected {len(selected)} chunk(s) for download test")

    return depot_id, [
        {"sha_hex": c.sha.hex(), "depot_id": depot_id}
        for c in selected
    ]



async def download_chunks(
    lancache_host: str, depot_id: int, chunks: list[dict], pass_label: str,
) -> list[dict]:
    """Download chunks through Lancache and return per-chunk result dicts."""
    results: list[dict] = []

    async with httpx.AsyncClient(
        base_url=f"http://{lancache_host}",
        headers={"Host": STEAM_VHOST, "User-Agent": STEAM_USER_AGENT},
        timeout=30.0,
    ) as http:
        for i, chunk in enumerate(chunks, 1):
            url = f"/depot/{chunk['depot_id']}/chunk/{chunk['sha_hex']}"
            t0 = time.monotonic()
            try:
                resp = await http.get(url)
                elapsed_ms = (time.monotonic() - t0) * 1000
                cache_status = resp.headers.get(CACHE_STATUS_HEADER, "UNKNOWN")
                size = len(resp.content)
                ok = resp.status_code == 200
                tag = "[OK]  " if ok else "[FAIL]"
                print(
                    f"  {tag} {pass_label} chunk {i}/{len(chunks)}: "
                    f"HTTP {resp.status_code}, cache={cache_status}, "
                    f"size={size}, time={elapsed_ms:.0f}ms"
                )
                results.append({
                    "sha_hex": chunk["sha_hex"], "status": resp.status_code,
                    "cache_status": cache_status, "content_length": size,
                    "elapsed_ms": elapsed_ms, "ok": ok,
                })
            except httpx.HTTPError as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                print(
                    f"  [FAIL] {pass_label} chunk {i}/{len(chunks)}: "
                    f"error={exc!r}, time={elapsed_ms:.0f}ms"
                )
                results.append({
                    "sha_hex": chunk["sha_hex"], "status": 0,
                    "cache_status": "ERROR", "content_length": 0,
                    "elapsed_ms": elapsed_ms, "ok": False,
                })
    return results


async def run_download_passes(
    lancache_host: str, depot_id: int, chunks: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Run two download passes and return (pass1_results, pass2_results)."""
    print("\n[INFO] === Pass 1: Cold cache (expect MISS) ===")
    pass1 = await download_chunks(lancache_host, depot_id, chunks, "Pass1")
    print("\n[INFO] === Pass 2: Warm cache (expect HIT) ===")
    pass2 = await download_chunks(lancache_host, depot_id, chunks, "Pass2")
    return pass1, pass2


def report_results(
    depot_id: int, chunks: list[dict],
    pass1: list[dict], pass2: list[dict],
    lancache_host: str,
) -> bool:
    """Print structured results and return True if overall PASS."""
    print("\n" + "=" * 65)
    print("SPIKE A — RESULTS SUMMARY")
    print("=" * 65)

    print(f"\n{'Chunk SHA (short)':<20} {'P1 Status':<12} {'P1 Cache':<10} "
          f"{'P2 Status':<12} {'P2 Cache':<10}")
    print("-" * 65)
    for r1, r2 in zip(pass1, pass2):
        sha_short = r1["sha_hex"][:16] + "..."
        print(f"{sha_short:<20} {'OK' if r1['ok'] else 'FAIL':<12} "
              f"{r1['cache_status']:<10} {'OK' if r2['ok'] else 'FAIL':<12} "
              f"{r2['cache_status']:<10}")

    for label, results in [("Pass 1", pass1), ("Pass 2", pass2)]:
        times = [r["elapsed_ms"] for r in results if r["ok"]]
        if times:
            print(f"\n{label} timing: min={min(times):.0f}ms, "
                  f"max={max(times):.0f}ms, avg={sum(times)/len(times):.0f}ms")

    example_sha = chunks[0]["sha_hex"] if chunks else "<sha_hex>"
    print(f"\n[INFO] URL pattern: http://{lancache_host}/depot/{depot_id}"
          f"/chunk/{example_sha}")
    print(f"       Host header: {STEAM_VHOST}")
    print(f"       User-Agent:  {STEAM_USER_AGENT}")
    print(f"\n[INFO] Lancache cache key formula:")
    print(f"       $cacheidentifier$uri$slice_range")
    print(f"       = steam/depot/{depot_id}/chunk/<sha_hex>bytes=0-10485759")
    print(f"       (cacheidentifier='steam' when UA='{STEAM_USER_AGENT}')")
    print(f"       (slice size depends on deployment: check CACHE_SLICE_SIZE in .env)")

    all_p1_ok = all(r["ok"] for r in pass1)
    all_p2_ok = all(r["ok"] for r in pass2)
    all_p2_hit = all(r["cache_status"] == "HIT" for r in pass2 if r["ok"])
    passed = all_p1_ok and all_p2_ok and all_p2_hit

    print(f"\n{'=' * 65}")
    print(f"OVERALL: [{'PASS' if passed else 'FAIL'}]")
    if not all_p1_ok:
        print("  - Some Pass 1 downloads failed")
    if not all_p2_ok:
        print("  - Some Pass 2 downloads failed")
    if not all_p2_hit:
        print("  - Some Pass 2 responses were not cache HITs")
    print("=" * 65)
    return passed


def main() -> None:
    args = parse_args()

    print("=" * 65)
    print("SPIKE A — Steam Auth + Lancache Chunk Download")
    print(f"  App ID:        {args.app_id}")
    print(f"  Lancache host: {args.lancache_host}")
    print(f"  Max chunks:    {args.max_chunks}")
    print(f"  Credentials:   {args.credentials_dir}")
    print("=" * 65)

    # Step 1: Authenticate
    client = steam_login(args.credentials_dir)

    # Step 2: Retrieve manifests and select chunks
    try:
        depot_id, chunks = get_chunks(client, args.app_id, args.max_chunks)
    except Exception as exc:
        print(f"[FAIL] Manifest retrieval failed: {exc}")
        client.logout()
        sys.exit(1)

    # Step 3: Download chunks through Lancache (async)
    try:
        pass1, pass2 = asyncio.run(
            run_download_passes(args.lancache_host, depot_id, chunks)
        )
    except Exception as exc:
        print(f"[FAIL] Chunk download failed: {exc}")
        client.logout()
        sys.exit(1)

    # Step 4: Report
    passed = report_results(depot_id, chunks, pass1, pass2, args.lancache_host)

    client.logout()
    print("\n[INFO] Steam client logged out.")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
