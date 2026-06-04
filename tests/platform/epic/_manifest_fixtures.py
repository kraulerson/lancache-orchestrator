"""Synthetic Epic binary-manifest builder for F6 parser tests.

Owns the byte layout so parser tests don't depend on a captured real manifest.
Builds an uncompressed (stored_as=0) manifest with a ManifestMeta block and a
ChunkDataList covering N chunks. Shared by test_manifest_parse + test_manifest_fetch.
"""

from __future__ import annotations

import struct
import zlib


def make_chunks(n: int) -> list[dict]:
    return [
        {
            "guid": (i + 1, 2, 3, 4),
            "hash": 100 + i,
            "sha": bytes([i]) * 20,
            "group": i % 100,
            "size": 500 + i,
        }
        for i in range(n)
    ]


def build_manifest(version: int, chunks: list[dict], *, compress: bool = False) -> bytes:
    # Body -----------------------------------------------------------------
    body = bytearray()

    # ManifestMeta: meta_data_version(u8) + feature_level(u32) + is_file_data(u8)
    #   + app_id(u32) + 4 FStrings + prereq_count(u32)=0
    meta = bytearray()
    meta += struct.pack("<B", 0)  # meta_data_version
    meta += struct.pack("<I", 17)  # feature_level
    meta += struct.pack("<B", 0)  # is_file_data
    meta += struct.pack("<I", 1234)  # app_id
    for s in ("App", "1.0", "x.exe", "cmd"):  # FStrings: utf-8, len includes NUL
        b = s.encode() + b"\x00"
        meta += struct.pack("<i", len(b)) + b
    meta += struct.pack("<I", 0)  # prereq_count
    meta_size = 4 + len(meta)
    body += struct.pack("<I", meta_size) + meta

    # ChunkDataList: cdl_version(u8) + count(u32) + per-column arrays
    n = len(chunks)
    cdl = bytearray()
    cdl += struct.pack("<B", 0)  # cdl_version
    cdl += struct.pack("<I", n)  # chunk count
    for c in chunks:
        cdl += struct.pack("<IIII", *c["guid"])
    for c in chunks:
        cdl += struct.pack("<Q", c["hash"])
    for c in chunks:
        cdl += c["sha"]
    for c in chunks:
        cdl += struct.pack("<B", c["group"])
    for _ in chunks:
        cdl += struct.pack("<I", 1048576)  # window_size
    for c in chunks:
        cdl += struct.pack("<q", c["size"])
    cdl_size = 4 + len(cdl)
    body += struct.pack("<I", cdl_size) + cdl

    uncompressed = bytes(body)
    if compress:
        payload = zlib.compress(uncompressed)
        stored_as = 0x01
    else:
        payload = uncompressed
        stored_as = 0x00

    # Header ---------------------------------------------------------------
    header = bytearray()
    header += struct.pack("<I", 0x44BEC00C)  # magic
    header += struct.pack("<I", 0)  # header_size placeholder
    header += struct.pack("<I", len(payload))  # data_size_compressed
    header += struct.pack("<I", len(uncompressed))  # data_size_uncompressed
    header += b"\x00" * 20  # sha hash
    header += struct.pack("<B", stored_as)  # stored_as (0x01 = zlib body)
    header += struct.pack("<I", version)  # version
    header[4:8] = struct.pack("<I", len(header))  # real header_size
    return bytes(header) + payload
