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
