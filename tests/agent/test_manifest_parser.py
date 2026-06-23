"""Tests for the SteamPrefill manifest (.bin) chunk-SHA parser."""

from __future__ import annotations

from pathlib import Path

from orchestrator.agent.manifest_parser import parse_chunk_shas

FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.bin"


def test_parses_expected_chunk_count():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert len(shas) == 60


def test_chunk_shas_are_40_lowercase_hex():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    for s in shas:
        assert len(s) == 40
        assert s == s.lower()
        int(s, 16)  # parses as hex


def test_known_sha_present():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert "05c4fb5c153fc90fb89a05689fcf9edc494c1323" in shas


def test_dedups_across_files():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert isinstance(shas, set)


def test_malformed_returns_empty_not_crash():
    assert parse_chunk_shas(b"\x00\x01\x02not-a-manifest") == set()


def _wrap_chunk_id(value: bytes) -> bytes:
    """Build a minimal Manifest→FileData→ChunkData→ChunkId protobuf for `value`
    (all lengths < 128 → single-byte varints). Wire-type-2 tag for field 1 = 0x0A."""
    chunkdata = b"\x0a" + bytes([len(value)]) + value
    filedata = b"\x0a" + bytes([len(chunkdata)]) + chunkdata
    return b"\x0a" + bytes([len(filedata)]) + filedata


def test_rejects_non_hex_and_wrong_length_chunk_ids():
    """COR-2 (review 2026-06-23): a ChunkId that isn't a 40-char lowercase-hex
    SHA1 must be dropped, not surfaced as a bogus SHA (which would derive a wrong
    cache key and report a false miss)."""
    valid = b"a" * 40
    non_hex = b"z" * 40  # 40 chars but not hex
    too_short = b"abc123"
    uppercase = b"A" * 40  # not lowercase
    buf = _wrap_chunk_id(valid) + _wrap_chunk_id(non_hex)
    buf += _wrap_chunk_id(too_short) + _wrap_chunk_id(uppercase)
    assert parse_chunk_shas(buf) == {"a" * 40}
