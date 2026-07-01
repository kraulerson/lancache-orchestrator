"""Parse DepotDownloader's raw .manifest (SteamKit2 ContentManifestPayload) ->
chunk SHA1 hex set. Sections are [u32 magic][u32 len][protobuf]; the payload
(magic 0x71F617D0) holds repeated FileMapping (field 1), each with repeated
ChunkData (field 6), each whose sha (field 1) is 20 raw bytes. Pure stdlib; a
malformed buffer yields an empty set (never raises). (DepotDownloader's
human-readable .txt has only file-level SHAs — not the chunks — so we parse the
binary; the existing parse_chunk_shas reads SteamPrefill's different .bin format.)"""

from __future__ import annotations

import struct

_PAYLOAD_MAGIC = 0x71F617D0


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


def _ld_fields(b: bytes) -> list[tuple[int, bytes]]:
    """[(field_num, payload)] for wire-type-2 fields; skip the rest."""
    i = 0
    n = len(b)
    out: list[tuple[int, bytes]] = []
    while i < n:
        tag, i = _read_varint(b, i)
        field, wire = tag >> 3, tag & 0x7
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
        else:
            break
    return out


def parse_steamkit_manifest(data: bytes) -> set[str]:
    shas: set[str] = set()
    i = 0
    try:
        while i + 8 <= len(data):
            magic, ln = struct.unpack_from("<II", data, i)
            i += 8
            body = data[i : i + ln]
            i += ln
            if magic != _PAYLOAD_MAGIC:
                continue
            for f1, filemap in _ld_fields(body):  # Payload.mappings
                if f1 != 1:
                    continue
                for f2, chunk in _ld_fields(filemap):  # FileMapping.chunks
                    if f2 != 6:
                        continue
                    for f3, val in _ld_fields(chunk):  # ChunkData.sha
                        if f3 == 1 and len(val) == 20:
                            shas.add(val.hex())
    except (IndexError, struct.error):
        return shas
    return shas
