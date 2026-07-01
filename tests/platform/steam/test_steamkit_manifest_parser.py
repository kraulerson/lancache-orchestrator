import struct

from orchestrator.platform.steam.steamkit_manifest_parser import parse_steamkit_manifest


def _tag(field, wire):
    return bytes([(field << 3) | wire])


def _ld(field, payload):
    return _tag(field, 2) + bytes([len(payload)]) + payload


def test_parse_extracts_chunk_sha1s_from_payload():
    sha_a = bytes.fromhex("aa" * 20)
    sha_b = bytes.fromhex("bb" * 20)
    chunk_a = _ld(1, sha_a)  # ChunkData.sha (field 1) = 20 raw bytes
    chunk_b = _ld(1, sha_b)
    filemap = _ld(6, chunk_a) + _ld(6, chunk_b)  # FileMapping.chunks (field 6), repeated
    payload = _ld(1, filemap)  # Payload.mappings (field 1)
    blob = struct.pack("<II", 0x71F617D0, len(payload)) + payload
    assert parse_steamkit_manifest(blob) == {"aa" * 20, "bb" * 20}


def test_parse_ignores_non_payload_sections_and_bad_input():
    assert parse_steamkit_manifest(b"") == set()
    assert parse_steamkit_manifest(b"\x00\x01\x02") == set()  # too short, no raise
