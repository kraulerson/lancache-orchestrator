"""Spike B -- Epic Games Lancache Prefill PoC.

Proves: Epic OAuth via httpx, manifest binary parsing, chunk download through
Lancache with Host-header routing, cache HIT verification on second pass.
Exploration script for Build Milestone A. NOT production code.

Dependencies: pip install httpx
Usage:
    python spike_b_epic_prefill.py --lancache-host 192.168.1.50
    python spike_b_epic_prefill.py --app-name Fortnite --max-chunks 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import httpx

# -- Constants ---------------------------------------------------------------
EPIC_TOKEN_URL = (
    "https://account-public-service-prod03.ol.epicgames.com"
    "/account/api/oauth/token"
)
EPIC_LIBRARY_URL = (
    "https://library-service.live.use1a.on.epicgames.com"
    "/library/api/public/items"
)
EPIC_MANIFEST_URL = (
    "https://launcher-public-service-prod06.ol.epicgames.com"
    "/launcher/api/public/assets/v2/platform/{platform}"
    "/namespace/{namespace}/catalogItem/{catalog_item_id}"
    "/app/{app_name}/label/{label}"
)
EPIC_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
EPIC_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
EPIC_LOGIN_URL = "https://legendary.gl/epiclogin"
EPIC_USER_AGENT = "EpicGamesLauncher/11.0.1-14907503+++Portal+Release-Live"


# -- Data classes ------------------------------------------------------------
@dataclass
class AuthTokens:
    access_token: str
    refresh_token: str
    display_name: str

@dataclass
class ChunkInfo:
    guid: str
    hash: int
    sha_hash: bytes
    group_num: int
    file_size: int
    window_size: int

@dataclass
class ManifestInfo:
    version: int
    chunks: list[ChunkInfo] = field(default_factory=list)
    cdn_url: str = ""

@dataclass
class DownloadResult:
    chunk_idx: int
    path: str
    status_code: int
    cache_status: str
    elapsed_ms: float
    size: int


# -- Authentication ----------------------------------------------------------
def authenticate(client: httpx.Client) -> AuthTokens:
    """Exchange an Epic authorization code for access tokens."""
    print(f"[INFO] Visit {EPIC_LOGIN_URL}")
    print("[INFO] Log in, then copy the authorization code shown on the page.")
    code = input("Paste authorization code: ").strip()
    if not code:
        print("[FAIL] No code entered."); sys.exit(1)

    resp = client.post(
        EPIC_TOKEN_URL,
        auth=(EPIC_CLIENT_ID, EPIC_CLIENT_SECRET),
        data={"grant_type": "authorization_code", "code": code, "token_type": "eg1"},
    )
    if resp.status_code != 200:
        print(f"[FAIL] Auth failed ({resp.status_code}): {resp.text}"); sys.exit(1)

    d = resp.json()
    tokens = AuthTokens(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token", ""),
        display_name=d.get("displayName", d.get("account_id", "unknown")),
    )
    print(f"[OK]   Authenticated as: {tokens.display_name}")
    return tokens


# -- Library enumeration -----------------------------------------------------
def list_library(client: httpx.Client, tokens: AuthTokens) -> list[dict[str, Any]]:
    """Fetch owned game library items (paginated)."""
    headers = {"Authorization": f"bearer {tokens.access_token}"}
    records: list[dict[str, Any]] = []
    params: dict[str, Any] = {"includeMetadata": True}

    while True:
        resp = client.get(EPIC_LIBRARY_URL, headers=headers, params=params)
        if resp.status_code != 200:
            print(f"[FAIL] Library fetch failed ({resp.status_code}): {resp.text}")
            sys.exit(1)
        data = resp.json()
        records.extend(data.get("records", []))
        cursor = data.get("responseMetadata", {}).get("nextCursor")
        if not cursor:
            break
        params["cursor"] = cursor

    print(f"[OK]   Found {len(records)} library items")
    return records


def pick_asset(records: list[dict[str, Any]], app_name: str | None) -> dict[str, Any]:
    """Select a library item by --app-name or interactive prompt."""
    if app_name:
        for rec in records:
            if rec.get("appName", "").lower() == app_name.lower():
                print(f"[OK]   Selected: {rec.get('appName')}")
                return rec
        print(f"[FAIL] App '{app_name}' not found in library.")
        sys.exit(1)

    show = records[:20]
    print("[INFO] First 20 library items:")
    for i, rec in enumerate(show):
        print(f"  [{i}] {rec.get('appName', '?')}  (ns={rec.get('namespace', '?')})")
    idx = int(input("Pick a number [0]: ").strip() or "0")
    print(f"[OK]   Selected: {show[idx].get('appName')}")
    return show[idx]


def get_manifest_url(
    client: httpx.Client, tokens: AuthTokens, record: dict[str, Any], platform: str,
) -> str:
    """Fetch manifest download URL for a library item via the v2 manifest API."""
    namespace = record.get("namespace", "")
    catalog_item_id = record.get("catalogItemId", "")
    app_name = record.get("appName", "")

    if not all([namespace, catalog_item_id, app_name]):
        print(f"[FAIL] Missing fields: ns={namespace}, cat={catalog_item_id}, app={app_name}")
        sys.exit(1)

    url = EPIC_MANIFEST_URL.format(
        platform=platform, namespace=namespace,
        catalog_item_id=catalog_item_id, app_name=app_name, label="Live",
    )
    resp = client.get(url, headers={"Authorization": f"bearer {tokens.access_token}"})
    if resp.status_code != 200:
        print(f"[FAIL] Manifest API failed ({resp.status_code}): {resp.text}")
        sys.exit(1)

    elements = resp.json().get("elements", [])
    if not elements:
        print("[FAIL] No manifest elements returned.")
        sys.exit(1)

    manifests = elements[0].get("manifests", [])
    if not manifests or not manifests[0].get("uri"):
        print("[FAIL] No manifest URI in response.")
        sys.exit(1)

    manifest = manifests[0]
    uri = manifest["uri"]
    if "queryParams" in manifest:
        params = "&".join(
            f"{p['name']}={p['value']}" for p in manifest["queryParams"]
        )
        uri = f"{uri}?{params}"

    return uri


# -- Manifest binary parsing -------------------------------------------------
def _read_fstring(bio: BytesIO) -> str:
    """Read an Unreal FString (int32 length + bytes; negative = UTF-16)."""
    (length,) = struct.unpack("<i", bio.read(4))
    if length == 0:
        return ""
    if length < 0:
        return bio.read(abs(length) * 2).decode("utf-16-le").rstrip("\x00")
    return bio.read(length).decode("utf-8", errors="replace").rstrip("\x00")


def _read_guid(bio: BytesIO) -> str:
    """Read 16-byte GUID, return 8-4-4-4-12 hex string."""
    raw = bio.read(16)
    a, b, c = struct.unpack("<IHH", raw[:8])
    t = raw[8:]
    return f"{a:08X}-{b:04X}-{c:04X}-{t[:2].hex().upper()}-{t[2:].hex().upper()}"


def _read_array(bio: BytesIO, count: int, fmt: str) -> list[Any]:
    """Read `count` values of struct format `fmt` from the stream."""
    size = struct.calcsize(fmt)
    return [struct.unpack(fmt, bio.read(size))[0] for _ in range(count)]


def parse_manifest(raw: bytes) -> ManifestInfo:
    """Parse a binary Epic manifest -- minimal, just enough for chunk URLs."""
    bio = BytesIO(raw)

    # Header
    (magic,) = struct.unpack("<I", bio.read(4))
    if magic != 0x44BEC00C:
        print(f"[FAIL] Bad manifest magic: {magic:#010x}"); sys.exit(1)
    (header_size,) = struct.unpack("<I", bio.read(4))
    bio.read(4)  # data_size_compressed
    bio.read(4)  # data_size_uncompressed
    bio.read(20)  # sha hash
    (stored_as,) = struct.unpack("<B", bio.read(1))
    (version,) = struct.unpack("<I", bio.read(4))
    print(f"[INFO] Manifest version: {version}, header_size: {header_size}")

    # Body (may be zlib-compressed)
    bio.seek(header_size)
    body_raw = bio.read()
    if stored_as & 0x01:
        body = zlib.decompress(body_raw)
        print(f"[INFO] Decompressed: {len(body_raw)} -> {len(body)} bytes")
    else:
        body = body_raw
    bb = BytesIO(body)

    # Manifest Meta -- read and skip
    (meta_size,) = struct.unpack("<I", bb.read(4))
    bb.read(1)  # meta_data_version
    bb.read(4)  # feature_level
    bb.read(1)  # is_file_data
    bb.read(4)  # app_id
    for _ in range(4):  # app_name, build_version, launch_exe, launch_cmd
        _read_fstring(bb)
    (prereq_count,) = struct.unpack("<I", bb.read(4))
    for _ in range(prereq_count * 4):  # 4 strings per prereq
        _read_fstring(bb)
    bb.seek(meta_size)

    # Chunk Data List
    bb.read(4)  # cdl_size
    (cdl_version,) = struct.unpack("<B", bb.read(1))
    (chunk_count,) = struct.unpack("<I", bb.read(4))
    print(f"[INFO] Chunk data list: {chunk_count} chunks (cdl_v{cdl_version})")

    guids = [_read_guid(bb) for _ in range(chunk_count)]
    hashes = _read_array(bb, chunk_count, "<Q")
    sha_hashes = [bb.read(20) for _ in range(chunk_count)]
    group_nums = _read_array(bb, chunk_count, "<B")
    window_sizes = _read_array(bb, chunk_count, "<I")
    file_sizes = _read_array(bb, chunk_count, "<q")

    manifest = ManifestInfo(version=version)
    for i in range(chunk_count):
        manifest.chunks.append(ChunkInfo(
            guid=guids[i], hash=hashes[i], sha_hash=sha_hashes[i],
            group_num=group_nums[i], file_size=file_sizes[i],
            window_size=window_sizes[i],
        ))
    total_mb = sum(c.file_size for c in manifest.chunks) / 1e6
    print(f"[OK]   Parsed {chunk_count} chunks, total size: {total_mb:.1f} MB")
    return manifest


def chunk_path(chunk: ChunkInfo, manifest_version: int) -> str:
    """Build the CDN-relative path for a chunk."""
    if manifest_version >= 22:
        import base64
        guid_bytes = bytes.fromhex(chunk.guid.replace("-", ""))
        h64 = base64.urlsafe_b64encode(chunk.hash.to_bytes(8, "little")).rstrip(b"=").decode()
        g64 = base64.urlsafe_b64encode(guid_bytes).rstrip(b"=").decode()
        return f"ChunksV5/{chunk.group_num:02d}/{h64}_{g64}.chunk"
    return f"ChunksV4/{chunk.group_num:02d}/{chunk.hash:016X}_{chunk.guid}.chunk"


# -- Chunk downloads through Lancache ----------------------------------------
async def download_chunks(
    chunks: list[ChunkInfo], manifest: ManifestInfo,
    lancache_host: str, cdn_base_url: str, pass_label: str,
) -> list[DownloadResult]:
    """Download chunks through the Lancache proxy."""
    parsed = urlparse(cdn_base_url)
    cdn_hostname = parsed.hostname or ""
    base_path = parsed.path.rsplit("/", 1)[0]
    print(f"[INFO] --- Pass: {pass_label} (host={cdn_hostname}) ---")

    results: list[DownloadResult] = []
    async with httpx.AsyncClient(
        base_url=f"http://{lancache_host}",
        headers={"Host": cdn_hostname, "User-Agent": EPIC_USER_AGENT},
        timeout=30.0,
    ) as client:
        for i, ch in enumerate(chunks):
            path = f"{base_path}/{chunk_path(ch, manifest.version)}"
            if i == 0:
                print(f"[DEBUG] First chunk URL path: {path}")
            t0 = time.monotonic()
            try:
                resp = await client.get(path)
                ms = (time.monotonic() - t0) * 1000
                cs = resp.headers.get("X-Upstream-Cache-Status", "UNKNOWN")
                results.append(DownloadResult(i, path, resp.status_code, cs, ms, len(resp.content)))
                tag = "[OK]  " if resp.status_code == 200 else "[FAIL]"
                print(f"  {tag} chunk {i}: {resp.status_code} cache={cs} {ms:.0f}ms {len(resp.content)}B")
                if resp.status_code != 200 and i == 0:
                    print(f"  [DEBUG] 403 body: {resp.text[:300]}")
                    direct_url = f"https://{cdn_hostname}{path}"
                    print(f"  [DEBUG] Trying direct CDN: {direct_url[:120]}...")
                    async with httpx.AsyncClient() as direct:
                        dr = await direct.get(direct_url, headers={"User-Agent": EPIC_USER_AGENT})
                        print(f"  [DEBUG] Direct result: {dr.status_code} {len(dr.content)}B")
            except httpx.HTTPError as exc:
                ms = (time.monotonic() - t0) * 1000
                results.append(DownloadResult(i, path, 0, "ERROR", ms, 0))
                print(f"  [FAIL] chunk {i}: {exc}")
    return results


# -- Results reporting -------------------------------------------------------
def report(
    auth_ok: bool, manifest_ok: bool,
    pass1: list[DownloadResult], pass2: list[DownloadResult],
    cdn_hostname: str, url_pattern: str,
) -> bool:
    """Print structured results and return overall pass/fail."""
    all_downloaded = all(r.status_code == 200 for r in pass1)
    all_hit = all(r.cache_status == "HIT" for r in pass2)
    overall = auth_ok and manifest_ok and all_downloaded and all_hit

    print("\n" + "=" * 60)
    print("SPIKE B RESULTS -- Epic Lancache Prefill")
    print("=" * 60)
    print(f"  Auth:           {'PASS' if auth_ok else 'FAIL'}")
    print(f"  Manifest parse: {'PASS' if manifest_ok else 'FAIL'}")
    print(f"  Pass 1 (MISS):  {'PASS' if all_downloaded else 'FAIL'}")
    print(f"  Pass 2 (HIT):   {'PASS' if all_hit else 'FAIL'}")
    if pass1:
        print(f"  Pass 1 avg:     {sum(r.elapsed_ms for r in pass1) / len(pass1):.0f} ms")
    if pass2:
        print(f"  Pass 2 avg:     {sum(r.elapsed_ms for r in pass2) / len(pass2):.0f} ms")
    print(f"  CDN hostname:   {cdn_hostname}")
    print(f"  URL pattern:    {url_pattern}")
    print(f"\n  OVERALL: {'PASS' if overall else 'FAIL'}")
    print("=" * 60)
    return overall


# -- Main --------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Spike B: Epic Lancache prefill PoC")
    parser.add_argument("--lancache-host", default=os.environ.get("LANCACHE_HOST", "lancache"),
                        help="Lancache proxy hostname/IP (default: $LANCACHE_HOST or 'lancache')")
    parser.add_argument("--app-name", default=None,
                        help="Specific Epic app name to test (omit to pick interactively)")
    parser.add_argument("--max-chunks", type=int, default=5,
                        help="Max chunks to download per pass (default: 5)")
    parser.add_argument("--platform", default="Windows", help="Platform (default: Windows)")
    parser.add_argument("--disable-https", action="store_true", default=True,
                        help="Use HTTP for CDN downloads (default: True, for Lancache)")
    args = parser.parse_args()

    auth_ok = manifest_ok = False
    pass1: list[DownloadResult] = []
    pass2: list[DownloadResult] = []
    cdn_hostname = url_pattern = ""

    try:
        # Step 1-3: Auth, list library, get manifest
        with httpx.Client() as client:
            tokens = authenticate(client)
            auth_ok = True
            records = list_library(client, tokens)
            record = pick_asset(records, args.app_name)
            manifest_url = get_manifest_url(client, tokens, record, args.platform)
            print(f"[INFO] Manifest URL: {manifest_url}")
            resp = client.get(manifest_url)
            if resp.status_code != 200:
                print(f"[FAIL] Manifest download failed: {resp.status_code}"); sys.exit(1)
            print(f"[OK]   Downloaded manifest: {len(resp.content)} bytes")

        # Step 4: Parse manifest
        manifest = parse_manifest(resp.content)
        manifest.cdn_url = manifest_url
        manifest_ok = True
        test_chunks = manifest.chunks[: args.max_chunks]
        print(f"[INFO] Testing with {len(test_chunks)} chunks")

        parsed_url = urlparse(manifest_url)
        cdn_hostname = parsed_url.hostname or ""
        url_pattern = f"http://{cdn_hostname}/.../{chunk_path(test_chunks[0], manifest.version)}"

        # Step 5: Download chunks (two passes)
        pass1 = asyncio.run(download_chunks(
            test_chunks, manifest, args.lancache_host, manifest_url, "1 (cold)"))
        pass2 = asyncio.run(download_chunks(
            test_chunks, manifest, args.lancache_host, manifest_url, "2 (warm)"))

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
    except Exception as exc:
        print(f"[FAIL] Unhandled error: {exc}")

    # Step 6: Report
    report(auth_ok, manifest_ok, pass1, pass2, cdn_hostname, url_pattern)


if __name__ == "__main__":
    main()
