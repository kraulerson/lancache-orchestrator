#!/usr/bin/env python3
"""Spike C -- Lancache Cache File Validator (proof-of-concept).

Proves the orchestrator can compute correct disk paths for Lancache nginx
cache files and verify their existence via stat(). Validates the core
algorithm for the production F7 cache validator.

Deps: Python 3.12+ (stdlib only), Spike A output (cached chunks on host).

Usage:
    python spike_c_validator.py --cache-root /mnt/lancache/cache \\
        --depot-id 228980 --chunk-sha abc123def456
    python spike_c_validator.py --cache-root /mnt/lancache/cache \\
        --uri /depot/228980/chunk/abc123 --content-length 5242880
    python spike_c_validator.py --cache-root /mnt/lancache/cache --scan-dir
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path

DEFAULT_SLICE_SIZE = 10_485_760  # 10 MiB — this Lancache deployment uses `slice 10m`


def compute_cache_path(
    cache_root: Path, cache_identifier: str, uri: str, slice_range: str,
) -> tuple[str, Path]:
    """Compute nginx cache key and on-disk path (core reusable algorithm)."""
    cache_key = f"{cache_identifier}{uri}{slice_range}"
    md5_hex = hashlib.md5(cache_key.encode()).hexdigest()
    # nginx levels=2:2: last 2 chars / chars[-4:-2] / full hash
    disk_path = cache_root / md5_hex[-2:] / md5_hex[-4:-2] / md5_hex
    return cache_key, disk_path


def slice_ranges(content_length: int, slice_size: int = DEFAULT_SLICE_SIZE) -> list[str]:
    """Generate all slice range strings for a given content length."""
    ranges = []
    offset = 0
    while offset < content_length:
        end = offset + slice_size - 1
        ranges.append(f"bytes={offset}-{end}")
        offset += slice_size
    return ranges


def extract_key_from_file(path: Path) -> str | None:
    """Read the nginx cache file header and extract the KEY line."""
    try:
        with path.open("rb") as f:
            header = f.read(4096)
        marker = b"\nKEY: "
        idx = header.find(marker)
        if idx == -1:
            return None
        start = idx + len(marker)
        end = header.find(b"\n", start)
        if end == -1:
            end = len(header)
        return header[start:end].decode("utf-8", errors="replace").strip()
    except OSError:
        return None


def verify_single_uri(
    cache_root: Path,
    cache_identifier: str,
    uri: str,
    content_length: int | None,
    slice_size: int = DEFAULT_SLICE_SIZE,
) -> bool:
    """Verify a single URI's cache presence. Returns True if PASS."""
    print(f"[INFO] Cache identifier : {cache_identifier}")
    print(f"[INFO] URI              : {uri}")
    print(f"[INFO] Slice size       : {slice_size} bytes ({slice_size // 1_048_576} MiB)")

    if content_length is not None:
        ranges = slice_ranges(content_length, slice_size)
        print(f"[INFO] Content length   : {content_length} bytes")
        print(f"[INFO] Expected slices  : {len(ranges)}")
    else:
        ranges = [f"bytes=0-{slice_size - 1}"]
        print("[INFO] Content length   : unknown (checking first slice only)")

    all_pass = True
    for sr in ranges:
        cache_key, disk_path = compute_cache_path(
            cache_root, cache_identifier, uri, sr,
        )
        print(f"  Slice {sr}")
        print(f"    Key  : {cache_key}")
        md5_hex = hashlib.md5(cache_key.encode()).hexdigest()
        print(f"    MD5  : {md5_hex}")
        print(f"    Path : {disk_path}")

        if disk_path.exists():
            stat = disk_path.stat()
            print(f"    Size : {stat.st_size} bytes")
            print(f"    Mtime: {stat.st_mtime}")
            extracted = extract_key_from_file(disk_path)
            if extracted is not None:
                print(f"    KEY  : {extracted}")
                if extracted == cache_key:
                    print("    [OK]   Extracted KEY matches computed key")
                else:
                    print("    [FAIL] KEY mismatch!")
                    print(f"           Expected: {cache_key}")
                    print(f"           Got:      {extracted}")
                    all_pass = False
            else:
                print("    [FAIL] Could not extract KEY line from file header")
                all_pass = False
        else:
            print("    [FAIL] File not found")
            all_pass = False
        print()

    return all_pass


def scan_cache_dir(cache_root: Path) -> None:
    """Walk the cache tree, extract keys, and report statistics."""
    total_files = 0
    total_size = 0
    no_key_count = 0
    identifiers: Counter[str] = Counter()
    depots: Counter[str] = Counter()

    print(f"[INFO] Scanning {cache_root} ...")
    for dirpath, _dirnames, filenames in os.walk(cache_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                stat = fpath.stat()
            except OSError:
                continue

            total_files += 1
            total_size += stat.st_size

            key = extract_key_from_file(fpath)
            if key is None:
                no_key_count += 1
                continue

            # Parse key: identifier + uri + slice_range (range starts with "bytes=")
            parts = key.rsplit("bytes=", maxsplit=1)
            if len(parts) != 2:
                no_key_count += 1
                continue
            # Split identifier from URI at first slash. For Steam the
            # identifier is "steam" and URI starts with "/".
            ident_uri = parts[0]
            slash_idx = ident_uri.find("/")
            if slash_idx > 0:
                ident, uri = ident_uri[:slash_idx], ident_uri[slash_idx:]
            else:
                ident, uri = ident_uri, ""
            identifiers[ident] += 1
            # Extract depot from Steam-style URIs
            if uri.startswith("/depot/"):
                uri_parts = uri.split("/")
                if len(uri_parts) >= 3:
                    depots[uri_parts[2]] += 1

    size_mb = total_size / (1024 * 1024)
    print(f"[INFO] Total files       : {total_files}")
    print(f"[INFO] Total size        : {size_mb:,.1f} MiB")
    print(f"[INFO] Files without KEY : {no_key_count}")
    if identifiers:
        print("[INFO] Breakdown by cache identifier:")
        for ident, count in identifiers.most_common():
            print(f"         {ident:<40s} {count:>8d} files")
    if depots:
        print("[INFO] Breakdown by Steam depot ID:")
        for depot, count in depots.most_common(20):
            print(f"         depot {depot:<20s} {count:>8d} files")


def build_uri(depot_id: str, chunk_sha: str) -> str:
    """Construct a Steam depot chunk URI from components."""
    return f"/depot/{depot_id}/chunk/{chunk_sha}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spike C — Lancache cache file path validator",
    )
    add = parser.add_argument
    add("--cache-root", required=True, type=Path,
        help="Host path to Lancache cache dir (e.g. /mnt/lancache/cache)")
    add("--cache-identifier", default="steam",
        help="Lancache cache identifier (default: steam)")
    add("--uri", help="Specific URI to check")
    add("--depot-id", help="Steam depot ID (alternative to --uri)")
    add("--chunk-sha", help="Steam chunk SHA (with --depot-id)")
    add("--content-length", type=int, default=None,
        help="Content length in bytes (verifies all slices if provided)")
    add("--slice-size", type=int, default=DEFAULT_SLICE_SIZE,
        help=f"Lancache slice size in bytes (default: {DEFAULT_SLICE_SIZE} = 10 MiB)")
    add("--scan-dir", action="store_true",
        help="Scan cache directory and report statistics")

    args = parser.parse_args()

    if not args.cache_root.is_dir():
        print(f"[FAIL] Cache root does not exist: {args.cache_root}")
        sys.exit(1)

    print("=" * 60)
    print("Spike C — Lancache Cache File Validator")
    print("=" * 60)
    print()
    print(f"[INFO] Cache root       : {args.cache_root}")
    print(f"[INFO] Formula          : md5({{identifier}}{{uri}}{{slice_range}})")
    print(f"[INFO] levels=2:2       : {{root}}/{{md5[-2:]}}/{{md5[-4:-2]}}/{{md5}}")
    print()

    if args.scan_dir:
        scan_cache_dir(args.cache_root)
        return

    # Resolve the URI
    uri = args.uri
    if uri is None and args.depot_id and args.chunk_sha:
        uri = build_uri(args.depot_id, args.chunk_sha)
    if uri is None:
        print("[FAIL] Provide --uri or both --depot-id and --chunk-sha, or --scan-dir")
        sys.exit(1)

    passed = verify_single_uri(
        args.cache_root, args.cache_identifier, uri, args.content_length,
        args.slice_size,
    )

    print("=" * 60)
    if passed:
        print("[OK]   Cache path computation VERIFIED")
    else:
        print("[FAIL] Cache path computation FAILED — see details above")
    print("=" * 60)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
