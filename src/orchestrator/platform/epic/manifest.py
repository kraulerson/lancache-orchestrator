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
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_MANIFEST_MAGIC = 0x44BEC00C
_MAX_CHUNKS = 5_000_000  # DoS guard on a corrupt/hostile chunk_count
_MAX_PREREQ = 100_000  # DoS guard on a corrupt/hostile prereq_count loop
# A plausible public FQDN (at least one dot). The CDN host comes from Epic's
# signed manifest response and is used as the lancache Host header (which routes
# the upstream fetch) — validate it so a hostile/MITM'd response can't point the
# lancache at an arbitrary bare-hostname internal target (adversarial review).
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class EpicManifestError(Exception):
    """Malformed or unsupported Epic manifest binary."""


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


def parse_manifest(raw: bytes) -> EpicManifest:
    """Parse an Epic binary manifest into version + chunk list."""
    try:
        return _parse(raw)
    except EpicManifestError:
        raise
    except (struct.error, zlib.error, ValueError, IndexError) as e:
        raise EpicManifestError(f"malformed Epic manifest: {type(e).__name__}: {e}") from e


def _parse(raw: bytes) -> EpicManifest:
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
    body = zlib.decompress(body_raw) if (stored_as & 0x01) else body_raw
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
            raise EpicManifestError(f"epic manifest API failed: HTTP {resp.status_code}")
        elements = resp.json().get("elements", [])
        if not elements:
            raise EpicManifestError("no manifest elements returned")
        manifests = elements[0].get("manifests", [])
        if not manifests or not manifests[0].get("uri"):
            raise EpicManifestError("no manifest URI in response")
        entry = manifests[0]
        uri = str(entry["uri"])
        params: dict[str, str] | None = None
        if "queryParams" in entry:
            params = {str(p["name"]): str(p["value"]) for p in entry["queryParams"]}
        mresp = await client.get(uri, params=params)
        if mresp.status_code != 200:
            raise EpicManifestError(f"epic manifest download failed: HTTP {mresp.status_code}")
        if len(mresp.content) > settings.manifest_size_cap_bytes:
            raise EpicManifestError(
                f"epic manifest exceeds size cap "
                f"({len(mresp.content)} > {settings.manifest_size_cap_bytes} bytes)"
            )
        manifest = parse_manifest(mresp.content)
        manifest.raw = mresp.content

    parsed = urlparse(uri)
    cdn_host = parsed.hostname or ""
    cdn_base = parsed.path.rsplit("/", 1)[0]
    # The CDN host/base come from Epic's signed response — validate before they
    # become the lancache Host header + URL path (adversarial review).
    if not _HOSTNAME_RE.match(cdn_host):
        raise EpicManifestError(f"implausible CDN host: {cdn_host!r}")
    if ".." in cdn_base:
        raise EpicManifestError(f"path traversal in CDN base: {cdn_base!r}")
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
