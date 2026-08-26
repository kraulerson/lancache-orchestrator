"""A half-written archive entry must not become permanent.

UAT-14 #292, second half. ``sync_manifests_to_archive`` copied straight to the final
archive filename and then skipped by filename forever:

    existing = {p.name for p in archive_v1.glob("*.bin")}
    ...
    if src.name in existing: continue
    shutil.copy2(src, archive_v1 / src.name)

So an interrupted copy — a full disk, a crash, an NFS hiccup mid-write — leaves a
truncated or zero-byte file under the real name, and it is never replaced even with a
complete source sitting in the live cache. Combined with the validator treating a
zero-chunk manifest as ``cached``, that made a false green self-perpetuating: the
sweep re-confirmed it every six hours.

Two properties fix it. The write is atomic, so a partial file never appears under the
final name at all; and a zero-byte entry already in the archive is treated as absent,
so an install that already suffered the bug heals itself on the next sync.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from unittest import mock

from orchestrator.agent.manifest_archive import sync_manifests_to_archive


def _live_manifest(live: Path, name: str, content: bytes, *, age_seconds: float = 60.0) -> Path:
    v1 = live / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    f = v1 / name
    f.write_bytes(content)
    # Backdate it past the settle window, or the sync skips it as mid-write.
    old = time.time() - age_seconds
    os.utime(f, (old, old))
    return f


def test_a_zero_byte_archive_entry_is_replaced(tmp_path: Path) -> None:
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"a complete manifest")

    archive_v1 = archive / "v1"
    archive_v1.mkdir(parents=True)
    (archive_v1 / "app.bin").write_bytes(b"")  # the wreckage of an interrupted copy

    copied = sync_manifests_to_archive(live, archive)

    assert copied == 1, "a zero-byte archive entry must not count as already present"
    assert (archive_v1 / "app.bin").read_bytes() == b"a complete manifest", (
        "the truncated entry must be replaced by the complete source, or the false "
        "green it causes is permanent"
    )


def test_a_complete_archive_entry_is_left_alone(tmp_path: Path) -> None:
    """The archive stays append-only for entries that are actually intact."""
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"newer content")

    archive_v1 = archive / "v1"
    archive_v1.mkdir(parents=True)
    (archive_v1 / "app.bin").write_bytes(b"original archived content")

    copied = sync_manifests_to_archive(live, archive)

    assert copied == 0
    assert (archive_v1 / "app.bin").read_bytes() == b"original archived content", (
        "an intact archive entry is the durable record and must never be overwritten"
    )


def test_no_temporary_file_survives_a_successful_sync(tmp_path: Path) -> None:
    """The archive holds the finished file and nothing else.

    HONEST SCOPE: this does not prove atomicity — it passes against the old
    straight-to-final-name copy too. Atomicity comes from os.replace being atomic
    within a filesystem, which is a property of the construction rather than
    something a test can observe without racing the write. What this DOES guard is
    the failure mode a temp-and-rename implementation introduces: leftover .partial
    files accumulating in the archive.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"x" * 4096)

    sync_manifests_to_archive(live, archive)

    entries = sorted(p.name for p in (archive / "v1").iterdir())
    assert entries == ["app.bin"], f"temporary files must not survive the sync: {entries}"
    assert (archive / "v1" / "app.bin").read_bytes() == b"x" * 4096


def test_a_failed_copy_leaves_nothing_behind(tmp_path: Path) -> None:
    """A failed copy leaves the archive unchanged, with no stub blocking a retry.

    HONEST SCOPE: with an unreadable source the copy fails before it creates
    anything, so this passes against the old implementation as well. It is kept as a
    guard on the new one — the temp file must be cleaned up on any error path, or a
    disk-full mid-copy would leave .partial files behind forever.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    src = _live_manifest(live, "app.bin", b"content")
    (archive / "v1").mkdir(parents=True)

    # Make the source unreadable so the copy fails partway through the attempt.
    src.chmod(0o000)
    try:
        copied = sync_manifests_to_archive(live, archive)
    finally:
        src.chmod(0o644)

    assert copied == 0
    assert list((archive / "v1").iterdir()) == [], (
        "a failed copy must not leave a stub — the next sync has to be free to retry"
    )


def test_concurrent_syncs_do_not_share_a_temp_name(tmp_path: Path) -> None:
    """Two callers copying the same manifest must not truncate each other's temp file.

    There genuinely are two: the 1800s background loop, and _capture_prefill_manifests
    calling this synchronously with settle_seconds=0 right after a prefill. With one
    fixed temp name per manifest they interleave — A finishes copying to tmp, B
    reopens and truncates the SAME tmp, A renames B's half-written file into place.
    A reader then sees a truncated manifest under the final name, which the atomicity
    comment claims is impossible, and if the process dies mid-way the final file is
    truncated with size > 0 — which the zero-byte heal does NOT catch.

    Asserted structurally: the temp name must vary between calls. Racing two real
    threads would make this flaky and prove less.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"content")

    seen: list[str] = []
    real_copy = shutil.copy2

    def spy(src, dst, *a, **kw):
        seen.append(Path(dst).name)
        return real_copy(src, dst, *a, **kw)

    with mock.patch.object(shutil, "copy2", spy):
        sync_manifests_to_archive(live, archive)
        (archive / "v1" / "app.bin").unlink()  # force a second copy of the same name
        sync_manifests_to_archive(live, archive)

    assert len(seen) == 2, f"expected two copies, saw {seen}"
    assert seen[0] != seen[1], (
        f"both copies used the temp name {seen[0]!r}. A concurrent caller would "
        "truncate the first one's file and the rename would publish a partial "
        "manifest under the final name."
    )


def test_a_stale_partial_from_a_killed_process_is_swept_up(tmp_path: Path) -> None:
    """Unique temp names fix the collision but introduce litter.

    The old fixed name at least self-overwrote on the next attempt. A unique one
    orphaned by SIGKILL mid-copy is never touched again, so the archive accumulates
    them indefinitely — on a store this loop stats every cycle.

    Only OLD ones are swept: a .partial younger than the settle window may belong to
    a copy running right now, and deleting it would break the very race the unique
    names exist to prevent.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"content")
    archive_v1 = archive / "v1"
    archive_v1.mkdir(parents=True)

    stale = archive_v1 / ".old.bin.999.deadbeef.partial"
    stale.write_bytes(b"orphaned by a kill")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    fresh = archive_v1 / ".other.bin.1000.cafebabe.partial"
    fresh.write_bytes(b"a copy in flight right now")

    sync_manifests_to_archive(live, archive)

    assert not stale.exists(), "an hours-old orphan must be swept up"
    assert fresh.exists(), (
        "a just-created .partial may belong to a concurrent copy — deleting it would "
        "reintroduce the truncation race the unique names exist to prevent"
    )
