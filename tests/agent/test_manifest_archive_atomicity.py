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

import structlog.testing

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
    anything, so no temp ever exists and this passes against every version of the
    implementation. It does NOT guard the cleanup in the except handler, despite an
    earlier version of this note claiming it did — deleting `tmp.unlink()` left the
    whole suite green until round 5 of review caught it. The real guard is
    test_a_copy_that_fails_after_creating_the_temp_cleans_it_up, which fails the
    copy AFTER the temp is on disk. What this one still pins is that a failure
    before any write leaves the archive untouched.
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
    real_copy = shutil.copyfile

    def spy(src, dst, *a, **kw):
        seen.append(Path(dst).name)
        return real_copy(src, dst, *a, **kw)

    # copyfile, not copy2 — copystat is copy2's last act, so copy2 handed back a
    # temp already carrying the source's stale mtime (see the staleness test).
    with mock.patch.object(shutil, "copyfile", spy):
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


def test_the_temp_file_is_not_stale_when_it_is_renamed(tmp_path: Path) -> None:
    """shutil.copy2 back-dates the temp file, which makes the sweep able to eat it.

    copy2 runs copystat, so the temp inherits the SOURCE's mtime — and manifests are
    deliberately backdated past the settle window before they are eligible to copy.
    The temp is therefore born looking hours old. For the one statement between copy2
    returning and os.replace, a concurrent sweep sees a stale .partial and unlinks it;
    os.replace then raises FileNotFoundError and the copy is lost for that cycle.

    Benign — the except OSError catches it and the next sync retries, nothing is
    truncated.

    NOTE: this docstring used to recommend stamping the temp to now before renaming
    "so it can never look stale while it is in use". That was wrong and PR #302
    disproved it: copystat is copy2's LAST act, so the stamp merely moved the
    exposure into the gap before itself, exactly as wide. The fix was to drop copy2
    for copyfile, which leaves the temp fresh from its first byte. See
    test_the_temp_is_never_stale_at_any_point, which asserts the stronger property.

    Asserted at the moment that matters, by inspecting the temp's age from inside
    os.replace.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    # Aged well past the sweep threshold, exactly as a real settled manifest is.
    _live_manifest(live, "app.bin", b"content", age_seconds=7200)

    ages: list[float] = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        ages.append(time.time() - os.stat(src).st_mtime)
        return real_replace(src, dst, *a, **kw)

    with mock.patch.object(os, "replace", spy):
        sync_manifests_to_archive(live, archive)

    assert ages, "no rename happened"
    assert ages[0] < 60, (
        f"the temp file was {ages[0]:.0f}s old at rename time — a concurrent sweep "
        "would treat it as an orphan and unlink it mid-flight"
    )


def test_the_archived_file_keeps_the_sources_mtime(tmp_path: Path) -> None:
    """Preserving mtime is load-bearing, not cosmetic.

    manifest_locator picks which manifest to validate against with
    `max(pool, key=lambda p: p.stat().st_mtime)` — newest wins. Stamping archived
    files to "now" would make every freshly-synced manifest the newest, changing
    which version validate compares against. That is the false-Partial bug class
    (UAT-13 F2), so the mtime the archive carries must remain the source's.

    This is the constraint that makes the in-flight freshness fix non-trivial: the
    TEMP must look new so the sweep spares it, while the ARCHIVED file must look old.

    HONEST SCOPE: this passes against the pre-change code too, where copy2 preserved
    the mtime on its own. It does not pin the current implementation — it guards
    against the NAIVE fix (stamp the temp and let the rename carry that mtime
    through), which is the one I actually wrote first and which this test caught.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    src = _live_manifest(live, "app.bin", b"content", age_seconds=7200)
    src_mtime = os.stat(src).st_mtime

    sync_manifests_to_archive(live, archive)

    archived_mtime = os.stat(archive / "v1" / "app.bin").st_mtime
    assert abs(archived_mtime - src_mtime) < 2, (
        f"archived mtime {archived_mtime} should track the source's {src_mtime}. "
        "manifest_locator selects the NEWEST manifest by mtime, so re-stamping the "
        "archive changes which version validate uses."
    )


def test_the_temp_is_never_stale_at_any_point(tmp_path: Path) -> None:
    """Freshness must hold from the first byte, not from a stamp applied afterwards.

    The first attempt at this used copy2 then os.utime(tmp). copystat is copy2's
    LAST act, so the temp was still born carrying the source's mtime and the sweep
    could still eat it — in the gap between copy2 returning and the utime, exactly
    as wide as the gap it was meant to close. The race moved; it did not go.

    copyfile does not call copystat, so the temp's mtime is its own write time:
    fresh by construction, and refreshed by every write during a slow NFS copy
    rather than only at the end.

    Asserted at the moment copy2's version was stale — immediately on return from
    the copy, before any stamping.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"content", age_seconds=7200)

    ages: list[float] = []
    real_copyfile = shutil.copyfile
    real_copy2 = shutil.copy2

    def spy_copyfile(src, dst, *a, **kw):
        out = real_copyfile(src, dst, *a, **kw)
        ages.append(time.time() - os.stat(dst).st_mtime)
        return out

    def spy_copy2(src, dst, *a, **kw):
        out = real_copy2(src, dst, *a, **kw)
        ages.append(time.time() - os.stat(dst).st_mtime)
        return out

    with (
        mock.patch.object(shutil, "copyfile", spy_copyfile),
        mock.patch.object(shutil, "copy2", spy_copy2),
    ):
        sync_manifests_to_archive(live, archive)

    # BOTH spies are installed because copy2 calls copyfile INTERNALLY — checking
    # only the first sample would read the inner copyfile's fresh mtime and pass
    # while the outer copy2 left it stale. The property is that the temp is fresh
    # after EVERY copy operation, so assert on the worst sample.
    assert ages, "no copy happened"
    assert max(ages) < 60, (
        f"the temp was {max(ages):.0f}s old on return from a copy — a sweep winning "
        "the next statement would unlink it, which is the race this was supposed to "
        "remove rather than relocate"
    )


def test_a_failed_mtime_restore_is_logged_not_swallowed(tmp_path: Path) -> None:
    """The restore is load-bearing, so its failure must be visible.

    If it fails after a successful rename, the archived file keeps the FRESH mtime
    permanently and outranks every sibling manifest at manifest_locator's
    max(pool, key=st_mtime) until a newer one lands — a persistent wrong-version
    selection, the UAT-13 F2 class. Suppressing OSError made that silent, against
    this project's own "no silent fallbacks, fail loud" rule and unlike every other
    error path in this function.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"content", age_seconds=7200)

    def failing_restore(path, times=None, **kw):
        # The restore is now the ONLY utime in this function — the temp no longer
        # needs stamping, because copyfile leaves it fresh by construction.
        raise OSError("simulated metadata failure")

    logs: list[dict] = []
    with mock.patch.object(os, "utime", failing_restore):
        with structlog.testing.capture_logs() as captured:
            sync_manifests_to_archive(live, archive)
        logs = list(captured)

    assert any("mtime_restore_failed" in str(entry.get("event", "")) for entry in logs), (
        "a failed restore leaves the archived manifest permanently outranking its "
        f"siblings for validation. It must say so. Logged: {[e.get('event') for e in logs]}"
    )


def test_a_copy_that_fails_after_creating_the_temp_cleans_it_up(tmp_path: Path) -> None:
    """The error path must unlink a temp that actually exists.

    Every other failure test in this file raises BEFORE the temp is created — a
    chmod-000 source and a spy that throws on entry both fail at open. So none of
    them exercises `tmp.unlink()` in the except handler, and deleting that line
    leaves the whole archive suite green. Round 5 of review caught the disclosure on
    test_a_failed_copy_leaves_nothing_behind claiming otherwise.

    This simulates the real shape: a disk filling mid-write, where the temp is on
    disk and partially written when the error lands.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"content")
    (archive / "v1").mkdir(parents=True)

    real_copyfile = shutil.copyfile

    def fail_after_creating(src, dst, *a, **kw):
        real_copyfile(src, dst, *a, **kw)  # the temp now exists on disk
        raise OSError(28, "No space left on device")

    with mock.patch.object(shutil, "copyfile", fail_after_creating):
        copied = sync_manifests_to_archive(live, archive)

    assert copied == 0
    leftovers = [p.name for p in (archive / "v1").iterdir()]
    assert leftovers == [], (
        f"the temp survived a mid-copy failure: {leftovers}. Without cleanup these "
        "accumulate until the orphan sweep's next pass, and the sweep is the backstop "
        "rather than the mechanism."
    )
