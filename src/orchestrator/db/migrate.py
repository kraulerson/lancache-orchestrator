from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"

_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum TEXT NOT NULL
);
"""


def _file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_migration_id(filename: str) -> int | None:
    m = re.match(r"^(\d{4})_", filename)
    return int(m.group(1)) if m else None


def _discover_migrations(direction: str = "up") -> list[tuple[int, str, Path]]:
    results: list[tuple[int, str, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if direction == "up" and p.name.endswith("_down.sql"):
            continue
        if direction == "down" and not p.name.endswith("_down.sql"):
            continue
        mid = _parse_migration_id(p.name)
        if mid is not None:
            name = p.stem.removesuffix("_down") if direction == "down" else p.stem
            results.append((mid, name, p))
    return results


def run_migrations(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_META_DDL)

    applied: dict[int, str] = {}
    for row in conn.execute("SELECT id, checksum FROM schema_migrations ORDER BY id"):
        applied[row[0]] = row[1]

    available = _discover_migrations("up")

    for mid, name, path in available:
        if mid in applied:
            current_checksum = _file_checksum(path)
            if current_checksum != applied[mid]:
                log.critical(
                    "migration_content_drift",
                    migration_id=mid,
                    name=name,
                    expected=applied[mid][:16],
                    actual=current_checksum[:16],
                )
                conn.close()
                sys.exit(1)
            continue

        max_applied = max(applied.keys()) if applied else 0
        if mid <= max_applied:
            continue

        checksum = _file_checksum(path)
        sql = path.read_text(encoding="utf-8")

        log.info("migration_applying", migration_id=mid, name=name)
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (id, name, checksum) VALUES (?, ?, ?)",
                (mid, name, checksum),
            )
            conn.commit()
            applied[mid] = checksum
            log.info("migration_applied", migration_id=mid, name=name)
        except Exception:
            log.critical("migration_failed", migration_id=mid, name=name, exc_info=True)
            conn.close()
            sys.exit(1)

    max_applied = max(applied.keys()) if applied else 0
    max_available = max(m[0] for m in available) if available else 0
    if max_applied > max_available:
        log.critical(
            "schema_version_ahead",
            applied_version=max_applied,
            code_version=max_available,
        )
        conn.close()
        sys.exit(1)

    conn.close()
    log.info("migrations_complete", applied_count=len(applied))
