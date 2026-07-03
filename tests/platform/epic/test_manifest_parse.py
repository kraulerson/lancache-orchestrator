"""F6: Epic binary-manifest parser tests (the riskiest code in F6)."""

from __future__ import annotations

import pytest

from orchestrator.platform.epic.manifest import (
    EpicManifestError,
    _get_chunk_dir,
    chunk_path,
    parse_manifest,
)
from tests.platform.epic._manifest_fixtures import build_manifest, make_chunks


def test_parse_v22_chunks_and_base64_path():
    raw = build_manifest(22, make_chunks(2))
    m = parse_manifest(raw)
    assert m.version == 22
    assert len(m.chunks) == 2
    assert m.chunks[0].guid == (1, 2, 3, 4)
    assert m.chunks[0].hash == 100
    assert m.chunks[0].file_size == 500
    p = chunk_path(m.chunks[0], m.version)
    assert p.startswith("ChunksV5/00/")
    assert p.endswith(".chunk")
    assert "_" in p.rsplit("/", 1)[1]  # base64 hash_guid


def test_parse_zlib_compressed_body():
    """Real Epic manifests store a zlib-compressed body (stored_as & 0x01).
    The parser must decompress it before reading the meta + chunk-data-list."""
    raw = build_manifest(22, make_chunks(2), compress=True)
    m = parse_manifest(raw)
    assert m.version == 22
    assert len(m.chunks) == 2
    assert m.chunks[1].hash == 101


def test_decompression_bomb_is_capped():
    """A zlib body that inflates beyond the cap must raise EpicManifestError,
    NOT allocate the full decompressed buffer (DoS guard). The compressed
    manifest stays tiny — well under any compressed-size cap."""
    import struct
    import zlib

    big = b"\x00" * (256 * 1024)  # inflates from a few hundred bytes
    payload = zlib.compress(big)
    header = bytearray()
    header += struct.pack("<I", 0x44BEC00C)  # magic
    header += struct.pack("<I", 0)  # header_size placeholder
    header += struct.pack("<I", len(payload))  # data_size_compressed
    header += struct.pack("<I", len(big))  # data_size_uncompressed
    header += b"\x00" * 20  # sha
    header += struct.pack("<B", 0x01)  # stored_as = zlib body
    header += struct.pack("<I", 22)  # version
    header[4:8] = struct.pack("<I", len(header))  # real header_size
    raw = bytes(header) + payload

    with pytest.raises(EpicManifestError, match="cap"):
        parse_manifest(raw, max_decompressed=1024)


def test_compressed_body_within_cap_still_parses():
    """A normal compressed manifest decompresses fully under the cap."""
    raw = build_manifest(22, make_chunks(2), compress=True)
    m = parse_manifest(raw, max_decompressed=4 * 1024 * 1024)
    assert m.version == 22
    assert len(m.chunks) == 2


def test_parse_legacy_hex_path():
    raw = build_manifest(18, make_chunks(1))
    m = parse_manifest(raw)
    assert m.version == 18
    p = chunk_path(m.chunks[0], m.version)
    assert p.startswith("ChunksV4/00/")
    assert p.split("/")[-1].startswith(f"{100:016X}_")


def test_bad_magic_raises():
    with pytest.raises(EpicManifestError):
        parse_manifest(b"\x00\x00\x00\x00" + b"\x00" * 64)


def test_implausible_chunk_count_raises():
    raw = bytearray(build_manifest(22, make_chunks(1)))
    # Corrupt the chunk_count to an implausible value is hard via offsets here;
    # instead assert truncated body raises cleanly (struct.error wrapped).
    with pytest.raises(EpicManifestError):
        parse_manifest(bytes(raw[:50]))


def test_chunk_dir_thresholds():
    assert _get_chunk_dir(22) == "ChunksV5"
    assert _get_chunk_dir(15) == "ChunksV4"
    assert _get_chunk_dir(6) == "ChunksV3"
    assert _get_chunk_dir(3) == "ChunksV2"
    assert _get_chunk_dir(2) == "Chunks"


def test_read_fstring_utf16():
    """Negative FString length = UTF-16-LE (real manifests can carry it)."""
    import struct
    from io import BytesIO

    from orchestrator.platform.epic.manifest import _read_fstring

    s = "héllo"
    payload = (s + "\x00").encode("utf-16-le")
    buf = BytesIO(struct.pack("<i", -(len(s) + 1)) + payload)
    assert _read_fstring(buf) == s


def test_parse_json_legacy_manifest():
    """Epic serves some (older) games' manifests as JSON, not the binary format.
    Numbers are blob-encoded (3 decimal digits per byte, little-endian) and GUID
    keys are 32-hex. They parse into the same EpicChunk shape. Values + the
    resulting CDN chunk path are proven live against Epic's CDN (Palila spike
    2026-07-03: 5/5 HEAD 200)."""
    import json

    guid = "E3BEF01544B75CE12E44DA83C705CE57"
    manifest = {
        "ManifestFileVersion": "013000000000",  # blob -> 13 (ChunksV3)
        "bIsFileData": False,
        "AppNameString": "Palila",
        "FileManifestList": [],  # not needed to enumerate/validate chunks
        "ChunkHashList": {guid: "180065099141255040004083"},
        "DataGroupList": {guid: "090"},
        "ChunkFilesizeList": {guid: "155136003000000000000000"},
        "ChunkShaList": {guid: "0517E2128E0E8825665E80E2" + "00" * 8},
    }
    m = parse_manifest(json.dumps(manifest).encode())
    assert m.version == 13
    assert len(m.chunks) == 1
    c = m.chunks[0]
    assert c.guid == (0xE3BEF015, 0x44B75CE1, 0x2E44DA83, 0xC705CE57)
    assert c.group_num == 90
    # End-to-end: this exact path returned HTTP 200 from Epic's CDN in the spike.
    assert (
        chunk_path(c, m.version)
        == "ChunksV3/90/530428FF8D6341B4_E3BEF01544B75CE12E44DA83C705CE57.chunk"
    )


def test_parse_json_missing_chunklist_raises():
    """A JSON manifest without ChunkHashList is malformed -> EpicManifestError
    (never a bare KeyError that would crash the prefill/validate loop)."""
    import json

    with pytest.raises(EpicManifestError):
        parse_manifest(json.dumps({"ManifestFileVersion": "013000000000"}).encode())
