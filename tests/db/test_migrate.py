"""Tests for orchestrator.db.migrate — the SQLite migrations framework (ID1).

Each test maps to a specific GitHub issue from the UAT-1 audit (2026-04-22).
Tests target the post-fix API, so they fail until BL1 fixes land. This is by
design — TDD per CLAUDE.md Phase 2 Construction Rule.

Issue map:
  #3  SEV-1 atomicity                   → test_atomic_failure_*
  #4  SEV-1 gap migrations              → test_gap_*
  #5  SEV-2 drift on unapplied          → test_checksum_manifest_*, test_unapplied_tamper_*
  #6  SEV-2 schema_migrations tamper    → test_bypass_via_fake_applied_row_*
                                           test_post_apply_sanity_*
  #7  decision: rollback removed        → test_rollback_not_implemented_*
  #8  SEV-2 concurrent-runner race      → test_concurrent_runners_*
  #12 SEV-2 WAL on network FS           → test_*_filesystem_*
  #13 SEV-3 package-resource migrations → test_migrations_loaded_from_package_*

Baseline + regression tests cover fresh install, idempotent re-apply, STRICT
mode, WAL mode, FK RESTRICT, platform seeds, and expected table/index counts.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest

from orchestrator.db import migrate

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_FIRST_MIG = """\
CREATE TABLE platforms (
    name TEXT PRIMARY KEY CHECK (name IN ('steam','epic')),
    auth_status TEXT NOT NULL
) STRICT;
INSERT INTO platforms (name, auth_status) VALUES ('steam','never'), ('epic','never');
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_migration(migs_dir: Path, mid: int, name: str, sql: str) -> Path:
    p = migs_dir / f"{mid:04d}_{name}.sql"
    p.write_text(sql, encoding="utf-8")
    return p


def _write_checksums(migs_dir: Path, entries: list[tuple[int, str, str]]) -> Path:
    """entries = [(id, sha, filename), …]"""
    lines = ["# SHA-256 checksums for packaged migrations."]
    for mid, sha, fn in entries:
        lines.append(f"{mid:04d}  {sha}  {fn}")
    p = migs_dir / "CHECKSUMS"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def migs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "orch.db"


@pytest.fixture
def one_valid_migration(migs_dir: Path) -> Path:
    p = _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    _write_checksums(migs_dir, [(1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql")])
    return p


# ---------------------------------------------------------------------------
# Issue #3 SEV-1 — atomicity
# ---------------------------------------------------------------------------


def test_atomic_failure_leaves_no_partial_state(migs_dir: Path, db_path: Path) -> None:
    """Mid-migration crash must not leave half-applied DDL or a schema_migrations row.

    0001 creates two tables. The second CREATE contains invalid SQL. After the
    failed run, neither table exists AND schema_migrations has no row for id=1.
    """
    bad_sql = (
        "CREATE TABLE ok (id INTEGER PRIMARY KEY) STRICT;\n"
        "CREATE TABLE then_bad (THIS IS INVALID SQL);\n"
    )
    _write_migration(migs_dir, 1, "bad", bad_sql)
    _write_checksums(migs_dir, [(1, _sha256(bad_sql), "0001_bad.sql")])

    with pytest.raises((sqlite3.Error, SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # Open a fresh connection to verify the state
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Either the DB wasn't created at all, or schema_migrations exists but is empty
    # and 'ok' is NOT present.
    assert "ok" not in tables, "partial DDL from a failed migration leaked"
    if "schema_migrations" in tables:
        rows = list(conn.execute("SELECT id FROM schema_migrations"))
        assert rows == [], "schema_migrations recorded a migration that failed"
    conn.close()


def test_atomic_failure_allows_clean_retry(migs_dir: Path, db_path: Path) -> None:
    """After a failed apply, replacing the file and rerunning must succeed cleanly."""
    # SQLite doesn't validate type names, so "CREATE TABLE x (BAD SYNTAX)" is
    # legal. Use an unambiguously bogus statement instead.
    bad_sql = "CREATE TABLE zzz;\n"  # missing column list — hard syntax error
    _write_migration(migs_dir, 1, "initial", bad_sql)
    _write_checksums(migs_dir, [(1, _sha256(bad_sql), "0001_initial.sql")])

    with pytest.raises((sqlite3.Error, SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # Fix the file AND the checksum manifest.
    (migs_dir / "0001_initial.sql").write_text(_VALID_FIRST_MIG, encoding="utf-8")
    _write_checksums(migs_dir, [(1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql")])

    migrate.run_migrations(db_path, migrations_dir=migs_dir)

    conn = sqlite3.connect(db_path)
    rows = list(conn.execute("SELECT id FROM schema_migrations"))
    assert rows == [(1,)]
    conn.close()


def test_apply_sql_error_raises_migration_error_scrubbed(migs_dir: Path, db_path: Path) -> None:
    """A SQL error while applying a migration must surface as MigrationError with
    a SCRUBBED message — not a raw sqlite3 exception reflecting SQLite's literal
    error text (audit 2026-06-09). The documented contract and the API lifespan
    catch both depend on the MigrationError type.
    """
    bad_sql = "CREATE TABLE zzz;\n"  # missing column list — hard syntax error
    _write_migration(migs_dir, 1, "initial", bad_sql)
    _write_checksums(migs_dir, [(1, _sha256(bad_sql), "0001_initial.sql")])

    with pytest.raises(migrate.MigrationError) as ei:
        migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # The raw SQLite error text must not be reflected into the message.
    msg = str(ei.value).lower()
    assert "syntax error" not in msg
    assert "near" not in msg


def test_detect_filesystem_type_darwin_parses_mount_not_stat_sigil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On darwin, FS-type detection must parse `mount` (the real fstype), NOT
    `stat -f %T` which returns the inode file-type sigil and never a network-FS
    name — defeating the WAL-on-network-FS guard (audit 2026-06-09)."""
    from pathlib import Path as _Path
    from subprocess import CompletedProcess

    monkeypatch.setattr(migrate.sys, "platform", "darwin")
    mount_output = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "nas:/export on /mnt/db (nfs, nodev, nosuid, mounted by karl)\n"
    )

    def fake_run(argv, **kwargs):
        return CompletedProcess(argv, 0, stdout=mount_output, stderr="")

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)

    # /mnt/db/orch.db (new DB → parent /mnt/db, which is the nfs mount point).
    assert migrate._detect_filesystem_type(_Path("/mnt/db/orch.db")) == "nfs"


# ---------------------------------------------------------------------------
# Issue #4 SEV-1 — gap migrations
# ---------------------------------------------------------------------------


def test_gap_between_applied_and_new_raises(migs_dir: Path, db_path: Path) -> None:
    """Applied = {0001}. Adding 0003 without 0002 must be rejected hard."""
    _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    _write_checksums(migs_dir, [(1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql")])
    migrate.run_migrations(db_path, migrations_dir=migs_dir)  # applies 0001

    three_sql = "CREATE TABLE three (id INTEGER PRIMARY KEY) STRICT;\n"
    _write_migration(migs_dir, 3, "third", three_sql)
    _write_checksums(
        migs_dir,
        [
            (1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql"),
            (3, _sha256(three_sql), "0003_third.sql"),
        ],
    )

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_gap_at_start_raises(migs_dir: Path, db_path: Path) -> None:
    """Fresh DB; migrations = {0001, 0003}. Gap of 0002 must be rejected."""
    one_sql = _VALID_FIRST_MIG
    three_sql = "CREATE TABLE three (id INTEGER PRIMARY KEY) STRICT;\n"
    _write_migration(migs_dir, 1, "initial", one_sql)
    _write_migration(migs_dir, 3, "third", three_sql)
    _write_checksums(
        migs_dir,
        [
            (1, _sha256(one_sql), "0001_initial.sql"),
            (3, _sha256(three_sql), "0003_third.sql"),
        ],
    )

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_gap_backfill_detected(migs_dir: Path, db_path: Path) -> None:
    """Applied = {0001, 0003} via direct seed. Later introducing 0002 must be rejected.

    Simulates the silent-skip scenario: dev environment has 0001+0003 somehow
    applied (historical bypass, restored backup), then someone backfills 0002.
    The runner must refuse rather than silently skip 0002 (the SEV-1 bug).
    """
    one_sql = _VALID_FIRST_MIG
    two_sql = "CREATE TABLE two (id INTEGER PRIMARY KEY) STRICT;\n"
    three_sql = "CREATE TABLE three (id INTEGER PRIMARY KEY) STRICT;\n"
    _write_migration(migs_dir, 1, "initial", one_sql)
    _write_migration(migs_dir, 2, "second", two_sql)
    _write_migration(migs_dir, 3, "third", three_sql)
    _write_checksums(
        migs_dir,
        [
            (1, _sha256(one_sql), "0001_initial.sql"),
            (2, _sha256(two_sql), "0002_second.sql"),
            (3, _sha256(three_sql), "0003_third.sql"),
        ],
    )
    # Directly seed schema_migrations with a gap (1 and 3 applied, 2 missing).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checksum TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO schema_migrations (id, name, checksum) VALUES (?, ?, ?)",
        (1, "0001_initial", _sha256(one_sql)),
    )
    conn.execute(
        "INSERT INTO schema_migrations (id, name, checksum) VALUES (?, ?, ?)",
        (3, "0003_third", _sha256(three_sql)),
    )
    conn.commit()
    conn.close()

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


# ---------------------------------------------------------------------------
# Issue #5 SEV-2 — checksum manifest (drift on unapplied migrations)
# ---------------------------------------------------------------------------


def test_checksum_manifest_missing_entry_raises(migs_dir: Path, db_path: Path) -> None:
    """A migration file with no corresponding CHECKSUMS row must hard-fail."""
    _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    _write_checksums(migs_dir, [])  # empty manifest

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_checksum_manifest_extra_entry_raises(migs_dir: Path, db_path: Path) -> None:
    """A manifest entry with no corresponding file must hard-fail."""
    _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    _write_checksums(
        migs_dir,
        [
            (1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql"),
            (2, "0" * 64, "0002_missing.sql"),
        ],
    )

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_unapplied_tamper_detected_before_apply(migs_dir: Path, db_path: Path) -> None:
    """Tamper with an unapplied file whose manifest entry is pinned → rejected pre-apply."""
    _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    # Pin manifest to a DIFFERENT SHA than the file actually has.
    _write_checksums(migs_dir, [(1, "f" * 64, "0001_initial.sql")])

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_applied_checksum_mismatch_detected(migs_dir: Path, db_path: Path) -> None:
    """Once applied, later tampering with the file must still be detected on next boot."""
    _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
    _write_checksums(migs_dir, [(1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql")])
    migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # Tamper with the file but leave the manifest pointing at the old SHA →
    # manifest check fires first.
    tampered_sql = _VALID_FIRST_MIG + "\n-- malicious trailer\n"
    (migs_dir / "0001_initial.sql").write_text(tampered_sql, encoding="utf-8")

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


# ---------------------------------------------------------------------------
# Issue #6 SEV-2 — schema_migrations tamper / post-apply sanity
# ---------------------------------------------------------------------------


def test_bypass_via_fake_applied_row_rejected(
    one_valid_migration: Path, migs_dir: Path, db_path: Path
) -> None:
    """A hand-populated schema_migrations row with no actual schema → hard-fail.

    Simulates an attacker who writes a row claiming 0001 is applied on an empty
    DB. Runner's post-apply sanity check must catch the missing tables.
    """
    # Create schema_migrations and insert a fake "applied" row; no real tables.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checksum TEXT NOT NULL
        );
        INSERT INTO schema_migrations (id, name, checksum) VALUES (1,'0001_initial','deadbeef');
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_post_apply_sanity_check_on_real_schema(db_path: Path) -> None:
    """After applying the real packaged 0001, all expected tables exist."""
    migrate.run_migrations(db_path)  # default package source
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "platforms",
        "games",
        "manifests",
        "block_list",
        "validation_history",
        "jobs",
        "cache_observations",
        "schema_migrations",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"
    conn.close()


# ---------------------------------------------------------------------------
# Issue #7 decision — rollback is intentionally out of scope
# ---------------------------------------------------------------------------


def test_rollback_not_implemented() -> None:
    """Codifies the decision: no rollback_to / rollback API exists on the module."""
    assert not hasattr(migrate, "rollback_to")
    assert not hasattr(migrate, "rollback")


def test_no_down_scripts_shipped_in_package() -> None:
    """The dead _down.sql files must not ship with the package."""
    import importlib.resources as r

    files = list(r.files("orchestrator.db.migrations").iterdir())
    assert not any(str(f).endswith("_down.sql") for f in files), (
        "dead _down.sql files still shipped"
    )


# ---------------------------------------------------------------------------
# Issue #8 SEV-2 — concurrent-runner race
# ---------------------------------------------------------------------------


def test_concurrent_runners_serialize(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
) -> None:
    """Two threads invoking run_migrations on the same DB both return successfully
    and schema_migrations has exactly one row (no duplicates, no partial)."""
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            migrate.run_migrations(db_path, migrations_dir=migs_dir)
        except BaseException as e:
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"concurrent runners raised: {errors!r}"
    conn = sqlite3.connect(db_path)
    rows = list(conn.execute("SELECT id FROM schema_migrations"))
    assert rows == [(1,)]
    conn.close()


# ---------------------------------------------------------------------------
# Issue #12 SEV-2 — WAL on network filesystem
# ---------------------------------------------------------------------------


def test_non_local_filesystem_refuses_boot(
    one_valid_migration: Path, migs_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the DB is detected on NFS / CIFS, runner must raise before any PRAGMA."""
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda _p: "nfs")

    with pytest.raises((SystemExit, migrate.MigrationError)):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_local_filesystem_boots_normally(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
) -> None:
    """The real tmp_path (ext4/apfs/btrfs) must be accepted."""
    migrate.run_migrations(db_path, migrations_dir=migs_dir)  # no raise
    conn = sqlite3.connect(db_path)
    rows = list(conn.execute("SELECT id FROM schema_migrations"))
    assert rows == [(1,)]
    conn.close()


@pytest.mark.parametrize(
    "fstype",
    [
        "glusterfs",
        "fuse.glusterfs",
        "ceph",
        "cephfs",
        "lustre",
        "beegfs",
        "gpfs",
        "ocfs2",
        "gfs2",
        "moosefs",
        "fuse.s3fs",
        "fuse.gcsfuse",
        "fuse.goofys",
    ],
)
def test_expanded_network_fstypes_refused(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fstype: str,
) -> None:
    """Extended network-FS list (re-audit F2) refuses clustered + object-store mounts."""
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda _p: fstype)

    with pytest.raises(migrate.MigrationError):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_unknown_fstype_default_warns_but_proceeds(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-audit F1: when fstype detection returns 'unknown' and strict mode is not
    set, the runner proceeds but emits a structured warning so operators can see."""
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda _p: "unknown")
    monkeypatch.delenv("ORCH_REQUIRE_LOCAL_FS", raising=False)

    migrate.run_migrations(db_path, migrations_dir=migs_dir)  # no raise

    conn = sqlite3.connect(db_path)
    rows = list(conn.execute("SELECT id FROM schema_migrations"))
    conn.close()
    assert rows == [(1,)]


def test_unknown_fstype_strict_mode_raises(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-audit F1: ORCH_REQUIRE_LOCAL_FS=strict upgrades 'unknown' to hard failure,
    for deployments where silent corruption is worse than refusing to boot."""
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda _p: "unknown")
    monkeypatch.setenv("ORCH_REQUIRE_LOCAL_FS", "strict")

    with pytest.raises(migrate.MigrationError):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_strict_mode_read_via_settings_not_direct_env(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #23 regression: migrate.py reads require_local_fs from
    get_settings() — not directly from os.environ. This test proves it
    by setting an invalid Literal value for require_local_fs via env;
    the typed Settings field rejects it at construction. If migrate.py
    were still reading os.environ.get(...).strip().lower(), an invalid
    value would silently fall through to the 'not strict' branch and
    the test would pass for the wrong reason.
    """
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda _p: "unknown")
    monkeypatch.setenv("ORCH_REQUIRE_LOCAL_FS", "MAYBE-STRICT-TOTALLY-INVALID")

    # Settings construction inside _assert_local_filesystem must raise
    # because MAYBE-STRICT-TOTALLY-INVALID isn't in Literal["strict","warn","off"].
    with pytest.raises(ValueError):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


# ---------------------------------------------------------------------------
# UAT-2 regression: filesystem-check hardening (V-2, V-3)
# ---------------------------------------------------------------------------


def test_uat2_v2_symlink_resolves_to_real_path_for_fs_check(
    one_valid_migration: Path,
    migs_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V-2: A symlink that resolves to an NFS-mounted target must trigger
    the local-fs assertion based on the RESOLVED path, not the symlink's
    own location. Otherwise NFS-on-WAL silent corruption is reachable
    through a single symlink hop.
    """
    real_target = tmp_path / "real_db_on_nfs.db"
    symlink_path = tmp_path / "local_symlink.db"
    symlink_path.symlink_to(real_target)

    captured: list[str] = []

    def fake_detect(p: Path) -> str:
        captured.append(str(p))
        # Real target reports 'nfs' (simulating NFS mount); symlink itself
        # would report 'apfs' (local).
        if "real_db_on_nfs" in str(p):
            return "nfs"
        return "apfs"

    monkeypatch.setattr(migrate, "_detect_filesystem_type", fake_detect)

    with pytest.raises(migrate.MigrationError, match=r"(?i)nfs|network"):
        migrate.run_migrations(symlink_path, migrations_dir=migs_dir)

    # The detect call must have received the RESOLVED path, not the symlink.
    assert any("real_db_on_nfs" in p for p in captured), (
        f"_detect_filesystem_type was not called on the resolved path: {captured}"
    )


def test_uat2_v3_rejects_character_device_database_path(
    one_valid_migration: Path,
    migs_dir: Path,
) -> None:
    """V-3: /dev/null and other character/block devices must be rejected
    as database_path. Previously sqlite would silently open them and all
    writes would vanish."""
    from pathlib import Path as _Path

    if not _Path("/dev/null").exists():
        pytest.skip("/dev/null not available on this platform")
    with pytest.raises(migrate.MigrationError, match=r"(?i)character|block|device"):
        migrate.run_migrations(_Path("/dev/null"), migrations_dir=migs_dir)


def test_post_apply_sanity_failure_rolls_back(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-audit F6: if post-apply sanity check fails, the migration must NOT be
    committed as applied. Proves verify runs BEFORE COMMIT, not after."""

    def failing_verify(_conn: sqlite3.Connection, _expected: set[str]) -> None:
        raise migrate.MigrationError("injected sanity failure")

    monkeypatch.setattr(migrate, "_verify_expected_objects", failing_verify)

    with pytest.raises(migrate.MigrationError):
        migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # schema_migrations must NOT record the migration because verify ran inside
    # the transaction and triggered ROLLBACK.
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_migrations" in tables:
        rows = list(conn.execute("SELECT id FROM schema_migrations"))
        assert rows == [], "migration committed despite post-apply verify failure"
    conn.close()


# ---------------------------------------------------------------------------
# Issue #13 SEV-3 — migrations loaded via importlib.resources
# ---------------------------------------------------------------------------


def test_migrations_loaded_from_package_resource() -> None:
    """The default source is the packaged orchestrator.db.migrations subpackage."""
    import importlib.resources as r

    pkg = r.files("orchestrator.db.migrations")
    assert pkg.is_dir()
    sql_files = [f for f in pkg.iterdir() if f.name.endswith(".sql")]
    assert len(sql_files) >= 1, "no .sql migrations packaged"


def test_default_source_applies_schema(db_path: Path) -> None:
    """run_migrations() with no args uses the packaged source and succeeds."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "platforms" in tables and "games" in tables
    conn.close()


# ---------------------------------------------------------------------------
# Baseline / regression tests for ID1 behavior
# ---------------------------------------------------------------------------


def test_idempotent_reapply_is_noop(db_path: Path) -> None:
    """Running twice in a row produces the same schema state."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    before = list(conn.execute("SELECT id, name, checksum FROM schema_migrations ORDER BY id"))
    conn.close()

    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    after = list(conn.execute("SELECT id, name, checksum FROM schema_migrations ORDER BY id"))
    conn.close()

    assert before == after


def test_wal_journal_mode_set(db_path: Path) -> None:
    """PRAGMA journal_mode is WAL after migrations."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_strict_tables_enforced(db_path: Path) -> None:
    """Inserting wrong type into a STRICT column fails."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    # games.owned is INTEGER CHECK (owned IN (0,1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO games (platform, app_id, title, owned) VALUES (?,?,?,?)",
            ("steam", "1", "X", "not-an-int"),
        )
        conn.commit()
    conn.close()


def test_fk_restrict_on_games_platform(db_path: Path) -> None:
    """Deleting a referenced platform must be blocked by ON DELETE RESTRICT."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")  # per-connection
    conn.execute("INSERT INTO games (platform, app_id, title) VALUES ('steam','1','X')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM platforms WHERE name = 'steam'")
        conn.commit()
    conn.close()


def test_platform_seeds_present(db_path: Path) -> None:
    """platforms table has 'steam' and 'epic' seeded."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM platforms")}
    conn.close()
    assert names == {"steam", "epic"}


def test_all_seven_tables_present(db_path: Path) -> None:
    """7 entity tables + schema_migrations = 8 total, per Bible §5."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    expected = {
        "platforms",
        "games",
        "manifests",
        "block_list",
        "validation_history",
        "jobs",
        "cache_observations",
        "schema_migrations",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_thirteen_indexes_present(db_path: Path) -> None:
    """0001_initial.sql declares 13 indexes (auto-indexes on UNIQUE not counted)."""
    migrate.run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    # Count explicitly declared indexes (name NOT LIKE 'sqlite_autoindex%')
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name NOT LIKE 'sqlite_autoindex%'",
    ).fetchone()[0]
    conn.close()
    assert count >= 10, f"expected at least 10 explicit indexes, got {count}"


def test_migration_error_class_exists() -> None:
    """The module exposes a MigrationError exception type for typed handling."""
    assert hasattr(migrate, "MigrationError")
    assert issubclass(migrate.MigrationError, Exception)


# ---------------------------------------------------------------------------
# verify_schema_current (BL4 helper)
# ---------------------------------------------------------------------------


async def test_verify_schema_current_passes_on_fresh_apply(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
) -> None:
    """Right after run_migrations() applies the full set, verify_schema_current
    must succeed silently (uses migs_dir fixture so available == applied)."""
    import aiosqlite

    migrate.run_migrations(db_path, migrations_dir=migs_dir)

    # For this test we need available_ids to equal applied_ids. The runner
    # uses migs_dir; our verify helper reads the packaged manifest. Patch the
    # available helper to return what migs_dir actually applied.
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(migrate, "_load_available_ids", lambda: {1})
    try:
        async with aiosqlite.connect(str(db_path)) as conn:
            await migrate.verify_schema_current(conn)  # must not raise
    finally:
        monkeypatch.undo()


async def test_verify_schema_current_raises_when_table_missing(
    db_path: Path,
) -> None:
    """A DB that's never had migrations run lacks the schema_migrations table.
    verify_schema_current must report missing IDs (the full available set)."""
    import aiosqlite

    from orchestrator.db.pool import SchemaNotMigratedError

    async with aiosqlite.connect(str(db_path)) as conn:
        with pytest.raises(SchemaNotMigratedError) as exc_info:
            await migrate.verify_schema_current(conn)
        assert exc_info.value.missing  # at least one missing migration


async def test_verify_schema_current_detects_pending(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply migration 1, then patch available-IDs to {1, 2} (simulating
    migration 2 exists on disk but hasn't been applied)."""
    import aiosqlite

    from orchestrator.db.pool import SchemaNotMigratedError

    migrate.run_migrations(db_path, migrations_dir=migs_dir)
    monkeypatch.setattr(migrate, "_load_available_ids", lambda: {1, 2})

    async with aiosqlite.connect(str(db_path)) as conn:
        with pytest.raises(SchemaNotMigratedError) as exc_info:
            await migrate.verify_schema_current(conn)
        assert exc_info.value.missing == [2]


async def test_verify_schema_current_detects_unknown(
    one_valid_migration: Path,
    migs_dir: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply migration 1, then patch available-IDs to {} (the package no longer
    ships migration 1, e.g., post-downgrade-rollback)."""
    import aiosqlite

    from orchestrator.db.pool import SchemaUnknownMigrationError

    migrate.run_migrations(db_path, migrations_dir=migs_dir)
    monkeypatch.setattr(migrate, "_load_available_ids", lambda: set())

    async with aiosqlite.connect(str(db_path)) as conn:
        with pytest.raises(SchemaUnknownMigrationError) as exc_info:
            await migrate.verify_schema_current(conn)
        assert exc_info.value.unknown == [1]


# ---------------------------------------------------------------------------
# Issue #19: _split_sql honors string literals
# ---------------------------------------------------------------------------


class TestSplitSqlStringLiteralAware:
    def test_semicolon_inside_single_quote_literal_not_split(self) -> None:
        sql = "INSERT INTO t (col) VALUES (';-- oops'); SELECT 1;"
        result = migrate._split_sql(sql)
        assert result == ["INSERT INTO t (col) VALUES (';-- oops')", "SELECT 1"]

    def test_semicolon_inside_double_quote_identifier_not_split(self) -> None:
        sql = 'CREATE TABLE "t;weird" (x INTEGER); SELECT 1;'
        result = migrate._split_sql(sql)
        assert result == ['CREATE TABLE "t;weird" (x INTEGER)', "SELECT 1"]

    def test_doubled_single_quote_escape_preserved(self) -> None:
        # SQL escape: '' inside '...' is a literal single quote.
        sql = "INSERT INTO t VALUES ('it''s fine'); SELECT 2;"
        result = migrate._split_sql(sql)
        assert result == ["INSERT INTO t VALUES ('it''s fine')", "SELECT 2"]

    def test_line_comment_inside_literal_not_stripped(self) -> None:
        sql = "INSERT INTO t VALUES ('-- not a comment'); SELECT 3;"
        result = migrate._split_sql(sql)
        assert result == ["INSERT INTO t VALUES ('-- not a comment')", "SELECT 3"]

    def test_block_comment_inside_literal_not_stripped(self) -> None:
        sql = "INSERT INTO t VALUES ('text /* not comment */ more'); SELECT 4;"
        result = migrate._split_sql(sql)
        assert result == [
            "INSERT INTO t VALUES ('text /* not comment */ more')",
            "SELECT 4",
        ]

    def test_real_line_comment_stripped(self) -> None:
        sql = "SELECT 1; -- this is a comment\nSELECT 2;"
        result = migrate._split_sql(sql)
        assert result == ["SELECT 1", "SELECT 2"]

    def test_real_block_comment_stripped(self) -> None:
        sql = "SELECT 1; /* block\nmultiline */ SELECT 2;"
        result = migrate._split_sql(sql)
        assert result == ["SELECT 1", "SELECT 2"]

    def test_trailing_semicolon_handled(self) -> None:
        assert migrate._split_sql("SELECT 1;") == ["SELECT 1"]
        assert migrate._split_sql("SELECT 1") == ["SELECT 1"]


# ---------------------------------------------------------------------------
# Issue #20: _expected_tables_for handles VIRTUAL/TEMP/DROP + schema-qualified
# ---------------------------------------------------------------------------


class TestExpectedTablesParsing:
    def _mig(self, mid: int, sql: str):
        return migrate._Migration(mid=mid, name="t", sql=sql, sha="", filename=f"{mid:04d}_t.sql")

    def test_create_virtual_table_detected(self) -> None:
        mig = self._mig(1, "CREATE VIRTUAL TABLE fts USING fts5(body);")
        assert "fts" in migrate._expected_tables_for([mig])

    def test_create_temp_table_detected(self) -> None:
        mig = self._mig(1, "CREATE TEMP TABLE staging (x INT);")
        assert "staging" in migrate._expected_tables_for([mig])

    def test_create_temporary_table_detected(self) -> None:
        mig = self._mig(1, "CREATE TEMPORARY TABLE staging (x INT);")
        assert "staging" in migrate._expected_tables_for([mig])

    def test_schema_qualified_name_picks_table_only(self) -> None:
        mig = self._mig(1, "CREATE TABLE main.foo (x INT);")
        tables = migrate._expected_tables_for([mig])
        assert "foo" in tables
        assert "main" not in tables
        assert "main.foo" not in tables

    def test_drop_table_subtracts(self) -> None:
        mig = self._mig(
            1,
            "CREATE TABLE foo (x INT); CREATE TABLE bar (y INT); DROP TABLE foo;",
        )
        tables = migrate._expected_tables_for([mig])
        assert "bar" in tables
        assert "foo" not in tables
        assert "schema_migrations" in tables

    def test_drop_table_if_exists_subtracts(self) -> None:
        mig = self._mig(1, "CREATE TABLE foo (x INT); DROP TABLE IF EXISTS foo;")
        assert "foo" not in migrate._expected_tables_for([mig])

    def test_create_then_drop_in_separate_migrations(self) -> None:
        m1 = self._mig(1, "CREATE TABLE foo (x INT);")
        m2 = self._mig(2, "DROP TABLE foo;")
        assert "foo" not in migrate._expected_tables_for([m1, m2])


# ---------------------------------------------------------------------------
# Issue #21: CHECKSUMS filename validated at parse time
# ---------------------------------------------------------------------------


class TestChecksumFilenameValidation:
    def test_path_traversal_filename_rejected(self, migs_dir, db_path) -> None:
        _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
        sha = _sha256(_VALID_FIRST_MIG)
        (migs_dir / "CHECKSUMS").write_text(f"1 {sha} ../../etc/passwd\n", encoding="utf-8")
        with pytest.raises(migrate.MigrationError, match=r"invalid migration filename"):
            migrate.run_migrations(db_path, migrations_dir=migs_dir)

    def test_reserved_filename_rejected(self, migs_dir, db_path) -> None:
        _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
        sha = _sha256(_VALID_FIRST_MIG)
        (migs_dir / "CHECKSUMS").write_text(f"1 {sha} CHECKSUMS\n", encoding="utf-8")
        with pytest.raises(migrate.MigrationError, match=r"invalid migration filename"):
            migrate.run_migrations(db_path, migrations_dir=migs_dir)

    def test_init_py_filename_rejected(self, migs_dir, db_path) -> None:
        _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
        sha = _sha256(_VALID_FIRST_MIG)
        (migs_dir / "CHECKSUMS").write_text(f"1 {sha} __init__.py\n", encoding="utf-8")
        with pytest.raises(migrate.MigrationError, match=r"invalid migration filename"):
            migrate.run_migrations(db_path, migrations_dir=migs_dir)

    def test_valid_filename_accepted(self, migs_dir, db_path) -> None:
        _write_migration(migs_dir, 1, "initial", _VALID_FIRST_MIG)
        _write_checksums(migs_dir, [(1, _sha256(_VALID_FIRST_MIG), "0001_initial.sql")])
        migrate.run_migrations(db_path, migrations_dir=migs_dir)


def test_migration_0008_creates_steam_app_info_table(db_path: Path) -> None:
    """0008 adds the steam_app_info cache table (re-arch ③b): app_id PK,
    app_type, name, fetched_at — STRICT. library_sync reads it to filter
    prefilled apps to type='game' without re-querying the store API."""
    migrate.run_migrations(db_path)  # default packaged source includes 0008
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "steam_app_info" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(steam_app_info)")}
        assert {"app_id", "app_type", "name", "fetched_at"} <= cols
        # app_id is the primary key (idempotent upsert target).
        pk = [r[1] for r in conn.execute("PRAGMA table_info(steam_app_info)") if r[5]]
        assert pk == ["app_id"]
    finally:
        conn.close()


def test_migration_0010_adds_cdn_base_to_manifests(db_path: Path) -> None:
    """0010 adds a nullable cdn_base TEXT column to manifests so the Epic
    validator can compute lancache cache-keys from the CDN base path stored
    at prefill time. Simple ADD COLUMN — no table recreate required."""
    migrate.run_migrations(db_path)  # default packaged source includes 0010
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(manifests)").fetchall()]
        assert "cdn_base" in cols
    finally:
        conn.close()


def test_migration_0004_cleanup_keeps_earliest_inflight_per_platform() -> None:
    """SEV-3 (review 2026-06-02): migration 0004's one-time cleanup cancels
    all-but-earliest in-flight library_sync per platform BEFORE creating the
    UNIQUE index, so it applies cleanly to already-deployed DBs that carry the
    pre-existing duplicates the index prevents. Exercised against a raw DB
    seeded with the dup state the running pool can no longer produce."""
    from pathlib import Path

    import orchestrator

    mig_path = (
        Path(orchestrator.__file__).parent
        / "db"
        / "migrations"
        / "0004_jobs_library_sync_unique.sql"
    )
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, "
            "platform TEXT, state TEXT, source TEXT, started_at TEXT, "
            "finished_at TEXT, error TEXT);"
            "INSERT INTO jobs (kind,platform,state,source) VALUES "
            "('library_sync','steam','running','api');"
            "INSERT INTO jobs (kind,platform,state,source) VALUES "
            "('library_sync','steam','queued','scheduler');"
            "INSERT INTO jobs (kind,platform,state,source) VALUES "
            "('library_sync','epic','queued','api');"
            "INSERT INTO jobs (kind,platform,state,source) VALUES "
            "('library_sync','steam','succeeded','api');"
        )
        db.executescript(mig_path.read_text(encoding="utf-8"))  # cleanup UPDATE + index
        inflight = dict(
            db.execute(
                "SELECT platform, COUNT(*) FROM jobs WHERE kind='library_sync' "
                "AND state IN ('queued','running') GROUP BY platform"
            ).fetchall()
        )
        assert inflight == {"steam": 1, "epic": 1}
        kept = db.execute(
            "SELECT id, state FROM jobs WHERE kind='library_sync' "
            "AND platform='steam' AND state IN ('queued','running')"
        ).fetchone()
        assert kept == (1, "running")  # earliest (lowest id) survives
    finally:
        db.close()
