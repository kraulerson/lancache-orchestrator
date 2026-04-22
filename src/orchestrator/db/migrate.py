"""SQLite migrations runner with atomicity, gap detection, checksum pinning,
post-apply sanity, concurrent-runner serialization, and network-FS refusal.

See `tests/db/test_migrate.py` for the contract.
"""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import re
import sqlite3
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator
    from importlib.resources.abc import Traversable

log = structlog.get_logger()

_MIGRATION_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_NETWORK_FSTYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "nfsv4",
        "cifs",
        "smb",
        "smbfs",
        "smb2",
        "smbfs2",
        "ncpfs",
        "afs",
        "coda",
        "webdav",
        "webdavfs",
        "fuse.sshfs",
        "fuse.davfs",
        "fuse.rclone",
        "fuse.cifs",
        "fuse.smb",
    }
)

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


class MigrationError(Exception):
    """Raised for any migrations-framework failure: gap detected, checksum mismatch,
    non-local filesystem, sanity-check failure, or a SQL error during apply."""


@dataclass(frozen=True)
class _Migration:
    mid: int
    name: str
    filename: str
    sql: str
    sha: str


# ---------------------------------------------------------------------------
# Filesystem type detection (GH issue #12)
# ---------------------------------------------------------------------------


def _detect_filesystem_type(path: Path) -> str:
    """Return the filesystem type for `path`. Best-effort, returns 'unknown' on
    failure. Tests monkeypatch this to simulate NFS/CIFS."""
    target = str(path if path.exists() else path.parent)
    try:
        if sys.platform.startswith("linux"):
            best = "unknown"
            best_len = -1
            with open("/proc/self/mountinfo", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if "-" not in parts:
                        continue
                    dash = parts.index("-")
                    mount_point = parts[4]
                    fstype = parts[dash + 1]
                    if (
                        target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
                    ) and len(mount_point) > best_len:
                        best = fstype
                        best_len = len(mount_point)
            return best
        if sys.platform == "darwin":
            # Fixed argv; 'target' is a local filesystem path validated upstream.
            # nosemgrep: dangerous-subprocess-use
            result = subprocess.run(  # noqa: S603 — fixed argv, absolute path, no shell
                ["/usr/bin/stat", "-f", "%T", target],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "unknown"


def _assert_local_filesystem(db_path: Path) -> None:
    """Raise MigrationError if `db_path` is on a known network filesystem.
    SQLite WAL mode is incompatible with networked mmap, which silently corrupts."""
    fstype = _detect_filesystem_type(db_path)
    if fstype in _NETWORK_FSTYPES:
        raise MigrationError(
            f"database path {db_path} is on '{fstype}' — WAL journal mode "
            f"requires a local filesystem (ext4, btrfs, xfs, apfs). "
            f"Move the DB to a local disk or mount.",
        )


# ---------------------------------------------------------------------------
# Migration discovery + checksum manifest (GH issues #5, #13)
# ---------------------------------------------------------------------------


def _package_migrations_root() -> Traversable:
    """The packaged migrations subpackage. Tests that want a filesystem path
    can pass `migrations_dir=` to `run_migrations` instead."""
    return resources.files("orchestrator.db.migrations")


def _iter_migration_files(source: Path | Traversable) -> Iterator[tuple[int, str, str, str]]:
    """Yield (id, name, filename, sql) in ascending id order from a source directory
    or package resource root. Skips CHECKSUMS and __init__.py."""
    entries: list[Traversable] = sorted(
        (e for e in source.iterdir() if e.is_file()),
        key=lambda e: e.name,
    )
    for entry in entries:
        fn = entry.name
        m = _MIGRATION_NAME_RE.match(fn)
        if not m:
            continue
        mid = int(m.group(1))
        name = m.group(2)
        sql = entry.read_text(encoding="utf-8")
        yield mid, name, fn, sql


def _load_migrations(source: Path | Traversable) -> list[_Migration]:
    result: list[_Migration] = []
    for mid, name, fn, sql in _iter_migration_files(source):
        sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        result.append(_Migration(mid=mid, name=name, filename=fn, sql=sql, sha=sha))
    # ascending id
    result.sort(key=lambda m: m.mid)
    # detect duplicate ids (e.g., 0001_a.sql and 0001_b.sql both present)
    seen: set[int] = set()
    for mig in result:
        if mig.mid in seen:
            raise MigrationError(f"duplicate migration id {mig.mid} in {source}")
        seen.add(mig.mid)
    return result


def _load_checksum_manifest(source: Path | Traversable) -> dict[int, tuple[str, str]]:
    """Parse the CHECKSUMS manifest into {id: (sha, filename)}."""
    entry: Traversable | Path
    if isinstance(source, Path):
        entry = source / "CHECKSUMS"
        if not entry.exists():
            raise MigrationError(f"CHECKSUMS manifest missing at {entry}")
        text = entry.read_text(encoding="utf-8")
    else:
        try:
            entry = source.joinpath("CHECKSUMS")
            text = entry.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as e:
            raise MigrationError("CHECKSUMS manifest missing in packaged migrations") from e

    result: dict[int, tuple[str, str]] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise MigrationError(f"CHECKSUMS line {lineno}: expected 3 fields, got {line!r}")
        id_str, sha, fn = parts
        try:
            mid = int(id_str)
        except ValueError as e:
            raise MigrationError(f"CHECKSUMS line {lineno}: non-integer id {id_str!r}") from e
        if not _SHA_RE.match(sha.lower()):
            raise MigrationError(f"CHECKSUMS line {lineno}: invalid sha256 {sha!r}")
        if mid in result:
            raise MigrationError(f"CHECKSUMS line {lineno}: duplicate id {mid}")
        result[mid] = (sha.lower(), fn)
    return result


def _verify_checksum_manifest(
    migrations: list[_Migration],
    manifest: dict[int, tuple[str, str]],
) -> None:
    """Cross-check migrations and the manifest. Every file must have a pinned
    entry with a matching SHA; no extra entries allowed."""
    file_ids = {m.mid for m in migrations}
    manifest_ids = set(manifest.keys())
    extras = manifest_ids - file_ids
    if extras:
        raise MigrationError(
            f"CHECKSUMS has entries for migrations not on disk: {sorted(extras)}",
        )
    missing = file_ids - manifest_ids
    if missing:
        raise MigrationError(
            f"migration files not in CHECKSUMS manifest: {sorted(missing)}",
        )
    for mig in migrations:
        pinned_sha, pinned_fn = manifest[mig.mid]
        if mig.sha != pinned_sha:
            raise MigrationError(
                f"migration {mig.mid} checksum mismatch: "
                f"file={mig.sha[:16]}..., manifest={pinned_sha[:16]}...",
            )
        if pinned_fn != mig.filename:
            raise MigrationError(
                f"migration {mig.mid} filename mismatch: "
                f"file={mig.filename!r}, manifest={pinned_fn!r}",
            )


# ---------------------------------------------------------------------------
# Gap detection (GH issue #4)
# ---------------------------------------------------------------------------


def _assert_no_gaps(applied: set[int], available: set[int]) -> None:
    """Combined view of applied and available migration IDs must be a contiguous
    range starting at 1. Applied rows with no corresponding file (schema ahead of
    code) are also rejected."""
    combined = applied | available
    if not combined:
        return
    expected = set(range(1, max(combined) + 1))
    missing = expected - combined
    if missing:
        raise MigrationError(f"migration gap detected: missing IDs {sorted(missing)}")
    orphan = applied - available
    if orphan:
        raise MigrationError(
            f"schema is ahead of code: applied ids {sorted(orphan)} "
            f"have no corresponding migration file",
        )


# ---------------------------------------------------------------------------
# Statement splitter (no string-literal semicolons in our migrations)
# ---------------------------------------------------------------------------


def _split_sql(script: str) -> list[str]:
    """Strip comments and split on `;`. Migration SQL must not contain
    semicolons inside string literals (DDL rarely does)."""
    stripped = re.sub(r"--[^\n]*", "", script)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    return [s.strip() for s in stripped.split(";") if s.strip()]


# ---------------------------------------------------------------------------
# Derive expected tables from migration SQL (GH issue #6)
# ---------------------------------------------------------------------------


def _expected_tables_for(migrations: list[_Migration]) -> set[str]:
    """Parse CREATE TABLE statements from each migration's SQL to produce the
    set of tables that must exist after those migrations are applied. Always
    includes schema_migrations (created by the runner itself)."""
    tables: set[str] = {"schema_migrations"}
    for mig in migrations:
        for stmt in _split_sql(mig.sql):
            for name in _CREATE_TABLE_RE.findall(stmt):
                tables.add(name)
    return tables


def _verify_expected_objects(conn: sqlite3.Connection, expected: set[str]) -> None:
    present = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )
    }
    missing = expected - present
    if missing:
        raise MigrationError(
            f"schema integrity failure: missing tables {sorted(missing)}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_META_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " id INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    " checksum TEXT NOT NULL"
    ")"
)


def run_migrations(
    db_path: str | os.PathLike[str],
    *,
    migrations_dir: Path | None = None,
) -> None:
    """Apply all pending migrations atomically.

    Args:
        db_path: Path to the SQLite database file. Created if absent.
        migrations_dir: Override source directory (tests). Defaults to the
            packaged `orchestrator.db.migrations` subpackage.

    Raises:
        MigrationError: on any of: non-local filesystem, malformed CHECKSUMS,
            checksum drift (applied or unapplied), missing manifest entry,
            migration gap, post-apply sanity failure, SQL error during apply.
    """
    db_path = Path(db_path)
    _assert_local_filesystem(db_path)

    source: Path | Traversable = (
        migrations_dir if migrations_dir is not None else _package_migrations_root()
    )

    migrations = _load_migrations(source)
    manifest = _load_checksum_manifest(source)
    _verify_checksum_manifest(migrations, manifest)

    # Open connection in autocommit mode so our explicit BEGIN IMMEDIATE
    # starts a real transaction and the DDL inside it doesn't auto-commit.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # PRAGMAs that must run outside any transaction.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA cache_size = -32000")
        conn.execute("PRAGMA busy_timeout = 5000")  # concurrent runner wait

        conn.execute(_META_DDL)

        # Single BEGIN IMMEDIATE wraps the entire read-and-apply pass. This
        # serializes concurrent runners cleanly: a second runner blocks on
        # BEGIN IMMEDIATE until the first commits, then re-reads applied_map
        # and sees the work is already done.
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied_rows = conn.execute(
                "SELECT id, checksum FROM schema_migrations ORDER BY id",
            ).fetchall()
            applied_map: dict[int, str] = {row[0]: row[1] for row in applied_rows}

            available_ids = {m.mid for m in migrations}
            applied_ids = set(applied_map.keys())
            _assert_no_gaps(applied_ids, available_ids)

            # Detect tamper on already-applied migration files.
            for mig in migrations:
                if mig.mid in applied_map and applied_map[mig.mid] != mig.sha:
                    raise MigrationError(
                        f"applied migration {mig.mid} has drifted from recorded "
                        f"checksum: file={mig.sha[:16]}..., "
                        f"recorded={applied_map[mig.mid][:16]}...",
                    )

            # Pre-apply tamper check: if schema_migrations claims migrations
            # are applied, the tables those migrations should have created
            # must exist.
            if applied_ids:
                applied_migs = [m for m in migrations if m.mid in applied_ids]
                _verify_expected_objects(conn, _expected_tables_for(applied_migs))

            for mig in migrations:
                if mig.mid in applied_map:
                    continue
                log.info("migration_applying", migration_id=mig.mid, name=mig.name)
                for stmt in _split_sql(mig.sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (id, name, checksum) VALUES (?, ?, ?)",
                    (mig.mid, f"{mig.mid:04d}_{mig.name}", mig.sha),
                )
                log.info("migration_applied", migration_id=mig.mid, name=mig.name)

            conn.execute("COMMIT")
        except Exception:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

        _verify_expected_objects(conn, _expected_tables_for(migrations))
    finally:
        conn.close()
    log.info("migrations_complete", applied_count=len(migrations))


def _cli() -> int:
    """Simple module entrypoint: `python -m orchestrator.db.migrate <db_path>`."""
    if len(sys.argv) != 2:
        log.error("migrate_cli_usage", argv=sys.argv)
        return 2
    try:
        run_migrations(sys.argv[1])
    except MigrationError as e:
        log.critical("migrations_failed", error=str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
