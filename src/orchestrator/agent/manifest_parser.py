"""Parse a SteamPrefill cached manifest (.bin) -> set of chunk SHA1s.

The .bin is protobuf-net of SteamPrefill's `Manifest`:
  Manifest:  [1] repeated FileData,  [2] manifest gid,  [4] depot id
  FileData:  [1] repeated ChunkData
  ChunkData: [1] ChunkId (lowercase-hex SHA1 string), [2] compressed length
So chunk SHAs = field-1 (FileData) -> field-1 (ChunkData) -> field-1 (hex string),
deduped. Proven byte-identical to ValvePython on 4 depots (spike 2026-06-21).
Pure stdlib -- no protobuf library, no ValvePython, no gevent.
"""

from __future__ import annotations

import re

# A Steam chunk id is a 40-char lowercase-hex SHA1. COR-2: anything else (a
# non-hex / wrong-length / uppercase ChunkId from a corrupt or unexpected buffer)
# must be dropped — surfacing it as a "SHA" would derive a wrong cache key and
# report a false miss.
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            break
        shift += 7
    return val, i


def _length_delimited_fields(b: bytes) -> list[tuple[int, bytes]]:
    """Return [(field_num, payload)] for wire-type-2 fields; skip the rest."""
    i = 0
    out: list[tuple[int, bytes]] = []
    n = len(b)
    while i < n:
        tag = b[i]
        i += 1
        field = tag >> 3
        wire = tag & 0x7
        if wire == 2:
            ln, i = _read_varint(b, i)
            out.append((field, b[i : i + ln]))
            i += ln
        elif wire == 0:
            _, i = _read_varint(b, i)
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:  # unknown wire type -- stop walking this level
            break
    return out


def parse_shas(text: str) -> set[str]:
    """Extract chunk SHA1s from a ``.shas`` sidecar manifest: one lowercase
    40-hex SHA1 per line. Blank/short/non-hex/uppercase lines are dropped
    (same COR-2 guard as the .bin parser). Returns the deduped set."""
    return {line.strip() for line in text.splitlines() if _SHA1_RE.match(line.strip())}


def parse_chunk_shas(data: bytes) -> set[str]:
    """Extract the deduped set of chunk SHA1 hex strings from a .bin. A
    malformed/unrecognized buffer yields an empty set (never raises)."""
    shas: set[str] = set()
    try:
        for f, filedata in _length_delimited_fields(data):  # Manifest.Files
            if f != 1:
                continue
            for cf, chunkdata in _length_delimited_fields(filedata):  # FileData.Chunks
                if cf != 1:
                    continue
                for idf, val in _length_delimited_fields(chunkdata):  # ChunkData.ChunkId
                    if idf == 1:
                        sha = val.decode("ascii", "replace")
                        if _SHA1_RE.match(sha):
                            shas.add(sha)
    except (IndexError, ValueError):
        return set()
    return shas
