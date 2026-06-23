"""Epic binary manifest parsing + chunk-path construction (F6).

Pure functions ported from spikes/spike_b_epic_prefill.py (PASS). No I/O. Parses
just enough of the Unreal/EGS manifest to build chunk CDN paths. Raises
EpicManifestError on malformed input (never sys.exit / never silently truncates).
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import structlog

from orchestrator.platform.epic.models import EpicChunk, EpicLibraryItem, EpicManifest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_MANIFEST_MAGIC = 0x44BEC00C
_MAX_CHUNKS = 5_000_000  # DoS guard on a corrupt/hostile chunk_count
_MAX_PREREQ = 100_000  # DoS guard on a corrupt/hostile prereq_count loop
# Hard cap on the DECOMPRESSED manifest body. zlib.decompress is otherwise
# unbounded — a tiny compressed body can inflate to gigabytes (decompression
# bomb / DoS), and the compressed-size cap in fetch_manifest does NOT bound the
# decompressed output. 256 MiB is far above any real manifest (which is further
# bounded by _MAX_CHUNKS) yet stops a bomb before it is allocated.
_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
# A plausible public FQDN (at least one dot). The CDN host comes from Epic's
# signed manifest response and is used as the lancache Host header (which routes
# the upstream fetch) — validate it so a hostile/MITM'd response can't point the
# lancache at an arbitrary bare-hostname internal target (adversarial review).
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class EpicManifestError(Exception):
    """Malformed or unsupported Epic manifest binary.

    ``status_code`` carries the upstream HTTP status when the failure came from
    the auth-bearing manifest API response (so EpicClient can force a token
    refresh + retry on 401)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _read_fstring(bio: BytesIO) -> str:
    (length,) = struct.unpack("<i", bio.read(4))
    if length == 0:
        return ""
    if length < 0:  # UTF-16
        return bio.read(abs(length) * 2).decode("utf-16-le").rstrip("\x00")
    return bio.read(length).decode("utf-8", errors="replace").rstrip("\x00")


def _read_guid(bio: BytesIO) -> tuple[int, int, int, int]:
    a, b, c, d = struct.unpack("<IIII", bio.read(16))
    return (a, b, c, d)


def _read_array(bio: BytesIO, count: int, fmt: str) -> list[int]:
    size = struct.calcsize(fmt)
    return [struct.unpack(fmt, bio.read(size))[0] for _ in range(count)]


def parse_manifest(raw: bytes, *, max_decompressed: int = _MAX_DECOMPRESSED_BYTES) -> EpicManifest:
    """Parse an Epic binary manifest into version + chunk list.

    ``max_decompressed`` caps the inflated body to defuse a decompression bomb.
    """
    try:
        return _parse(raw, max_decompressed)
    except EpicManifestError:
        raise
    except (struct.error, zlib.error, ValueError, IndexError) as e:
        raise EpicManifestError(f"malformed Epic manifest: {type(e).__name__}: {e}") from e


def _decompress_capped(body_raw: bytes, max_bytes: int) -> bytes:
    """zlib-inflate ``body_raw`` with a hard output cap (anti-bomb).

    ``decompressobj().decompress(data, max_length)`` stops after ``max_length``
    output bytes, leaving the remainder in ``unconsumed_tail``; a non-empty tail
    (or ``not eof``) means the stream exceeds the cap (or is truncated), so we
    raise instead of allocating an unbounded buffer.
    """
    dobj = zlib.decompressobj()
    body = dobj.decompress(body_raw, max_bytes)
    if dobj.unconsumed_tail or not dobj.eof:
        raise EpicManifestError(f"epic manifest decompresses beyond size cap ({max_bytes} bytes)")
    return body


def _parse(raw: bytes, max_decompressed: int) -> EpicManifest:
    bio = BytesIO(raw)
    (magic,) = struct.unpack("<I", bio.read(4))
    if magic != _MANIFEST_MAGIC:
        raise EpicManifestError(f"bad manifest magic: {magic:#010x}")
    (header_size,) = struct.unpack("<I", bio.read(4))
    bio.read(4)  # data_size_compressed
    bio.read(4)  # data_size_uncompressed
    bio.read(20)  # sha hash
    (stored_as,) = struct.unpack("<B", bio.read(1))
    (version,) = struct.unpack("<I", bio.read(4))

    bio.seek(header_size)
    body_raw = bio.read()
    body = _decompress_capped(body_raw, max_decompressed) if (stored_as & 0x01) else body_raw
    bb = BytesIO(body)

    # ManifestMeta — read meta_size, then skip to its end.
    (meta_size,) = struct.unpack("<I", bb.read(4))
    if meta_size > len(body):
        raise EpicManifestError(f"meta_size {meta_size} exceeds body {len(body)}")
    bb.read(1)  # meta_data_version
    bb.read(4)  # feature_level
    bb.read(1)  # is_file_data
    bb.read(4)  # app_id
    for _ in range(4):  # app_name, build_version, launch_exe, launch_cmd
        _read_fstring(bb)
    (prereq_count,) = struct.unpack("<I", bb.read(4))
    if prereq_count > _MAX_PREREQ:
        raise EpicManifestError(f"implausible prereq_count {prereq_count}")
    for _ in range(prereq_count * 4):
        _read_fstring(bb)
    bb.seek(meta_size)

    # ChunkDataList
    bb.read(4)  # cdl_size
    bb.read(1)  # cdl_version
    (chunk_count,) = struct.unpack("<I", bb.read(4))
    if chunk_count > _MAX_CHUNKS:
        raise EpicManifestError(f"implausible chunk_count {chunk_count}")

    guids = [_read_guid(bb) for _ in range(chunk_count)]
    hashes = _read_array(bb, chunk_count, "<Q")
    sha_hashes = [bb.read(20) for _ in range(chunk_count)]
    group_nums = _read_array(bb, chunk_count, "<B")
    window_sizes = _read_array(bb, chunk_count, "<I")
    file_sizes = _read_array(bb, chunk_count, "<q")

    chunks = [
        EpicChunk(
            guid=guids[i],
            hash=hashes[i],
            sha_hash=sha_hashes[i],
            group_num=group_nums[i],
            file_size=file_sizes[i],
            window_size=window_sizes[i],
        )
        for i in range(chunk_count)
    ]
    return EpicManifest(version=version, chunks=chunks)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject httpx.MockTransport. None -> real network."""
    return None


def _client(settings: Settings) -> httpx.AsyncClient:
    transport = _build_transport()
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(60.0, connect=10.0),
        "headers": {"User-Agent": settings.epic_user_agent},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


async def _read_body_capped(chunks: AsyncIterator[bytes], cap: int) -> bytes:
    """Accumulate a streamed response body, raising EpicManifestError as soon as
    the running total exceeds ``cap``. NEW-4: enforce the manifest size cap WHILE
    downloading so an oversized (potentially OOM-sized) body from a hostile or
    misbehaving CDN is rejected early — never buffered in full first."""
    buf = bytearray()
    async for chunk in chunks:
        buf.extend(chunk)
        if len(buf) > cap:
            raise EpicManifestError(f"epic manifest exceeds size cap (> {cap} bytes)")
    return bytes(buf)


async def fetch_manifest(
    access_token: str, item: EpicLibraryItem, settings: Settings
) -> tuple[EpicManifest, str, str]:
    """Resolve a library item to a parsed manifest + CDN host + CDN base path.

    Epic's manifest/CDN URIs are signed and short-lived, so this is called fresh
    at prefill time (never reuse a stored signed URI). Returns
    ``(EpicManifest, cdn_host, cdn_base_path)``.
    """
    headers = {"Authorization": f"bearer {access_token}"}
    url = settings.epic_manifest_url_template.format(
        platform=settings.epic_platform,
        namespace=item.namespace,
        catalog_item_id=item.catalog_item_id,
        app_name=item.app_name,
        label=settings.epic_manifest_label,
    )
    async with _client(settings) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            # The auth-bearing API call — carry the status so a 401 forces a
            # token refresh + retry upstream.
            raise EpicManifestError(
                f"epic manifest API failed: HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        elements = resp.json().get("elements", [])
        if not elements:
            raise EpicManifestError("no manifest elements returned")
        manifests = elements[0].get("manifests", [])
        if not manifests or not manifests[0].get("uri"):
            raise EpicManifestError("no manifest URI in response")
        entry = manifests[0]
        uri = str(entry["uri"])
        # Validate the signed CDN URI BEFORE fetching it. ``uri`` comes from
        # Epic's response and is BOTH this GET's target AND the lancache Host
        # header + URL path. Validating after the GET (the prior bug) left the
        # manifest fetch itself open to SSRF — a hostile/MITM'd response could
        # point this GET at an internal host/IP (UAT-10 #4 / adversarial review).
        parsed = urlparse(uri)
        cdn_host = parsed.hostname or ""
        cdn_base = parsed.path.rsplit("/", 1)[0]
        if not _HOSTNAME_RE.match(cdn_host):
            raise EpicManifestError(f"implausible CDN host: {cdn_host!r}")
        if ".." in cdn_base:
            raise EpicManifestError(f"path traversal in CDN base: {cdn_base!r}")
        params: dict[str, str] | None = None
        if "queryParams" in entry:
            params = {str(p["name"]): str(p["value"]) for p in entry["queryParams"]}
        async with client.stream("GET", uri, params=params) as mresp:
            if mresp.status_code != 200:
                raise EpicManifestError(f"epic manifest download failed: HTTP {mresp.status_code}")
            # Stream + cap incrementally so an oversized body never OOMs us (NEW-4).
            content = await _read_body_capped(mresp.aiter_bytes(), settings.manifest_size_cap_bytes)
        manifest = parse_manifest(content)
        manifest.raw = content
        manifest.cdn_base = cdn_base
    return manifest, cdn_host, cdn_base


def _get_chunk_dir(version: int) -> str:
    if version >= 22:
        return "ChunksV5"
    if version >= 15:
        return "ChunksV4"
    if version >= 6:
        return "ChunksV3"
    if version >= 3:
        return "ChunksV2"
    return "Chunks"


def _guid_hex(guid: tuple[int, int, int, int]) -> str:
    return "".join(f"{g:08X}" for g in guid)


def chunk_path(chunk: EpicChunk, manifest_version: int) -> str:
    """CDN-relative chunk path (matches legendary). v>=22 uses base64 names."""
    chunk_dir = _get_chunk_dir(manifest_version)
    if manifest_version >= 22:
        h64 = base64.urlsafe_b64encode(struct.pack("<Q", chunk.hash)).rstrip(b"=").decode()
        g64 = base64.urlsafe_b64encode(struct.pack("<IIII", *chunk.guid)).rstrip(b"=").decode()
        return f"{chunk_dir}/{chunk.group_num:02d}/{h64}_{g64}.chunk"
    return f"{chunk_dir}/{chunk.group_num:02d}/{chunk.hash:016X}_{_guid_hex(chunk.guid)}.chunk"
