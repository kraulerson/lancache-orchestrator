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
