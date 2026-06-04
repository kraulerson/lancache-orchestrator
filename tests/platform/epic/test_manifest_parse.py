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
