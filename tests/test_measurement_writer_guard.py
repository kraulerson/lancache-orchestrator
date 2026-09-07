"""Fails the build if any module other than jobs/measurement.py writes cache truth.

This is the structural guarantee behind the 2026-09-04 design: a job outcome can
never again be recorded as a cache-content finding, because only one function is
permitted to write ``games.status`` / ``games.status_measured_at``.

The scan is deliberately source-level rather than runtime: a regression is a new
``UPDATE games SET status=...`` somebody types into a handler, and no test that
exercises today's code paths would ever run it.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
ALLOWED = {SRC / "jobs" / "measurement.py"}

# Python joins adjacent string literals, so one SQL statement is routinely spelled
# as `"... owned = 1, " "status = CASE ..."`. Collapse the seam before matching or
# the assignment hides behind a quote (this is exactly how library_sync's
# not_downloaded reset escaped the first draft of this guard). Comments are
# stripped first, because this repo's dominant idiom interleaves `#` notes
# BETWEEN the literals of one statement.
_LITERAL_SEAM = re.compile(r"(['\"])\s*\1")

# Both heads that assign columns: a plain UPDATE and the ON CONFLICT upsert form.
# The body stops at WHERE, which is what spares a legitimate `UPDATE games SET
# last_job_outcome=? WHERE status='downloading'` (the boot reaper) — only an
# assignment to a truth column is a hit. `'` is deliberately NOT excluded from
# the body: an inline SQL literal earlier in the same SET clause
# (`SET last_error='boom', status='failed'`) must not hide what follows it.
CACHE_TRUTH_WRITE = re.compile(
    r"(?:UPDATE\s+games\s+SET|DO\s+UPDATE\s+SET)"
    r"(?:(?!\bWHERE\b)[^\";])*?"
    r"\b(?:status|status_measured_at)\s*=",
    re.IGNORECASE,
)

# An INSERT needs no UPDATE anywhere to write cache truth: library_sync already
# inserts into `games`, so adding `status` to one of those column lists would
# have created a new row with a status nobody measured, invisible to the pattern
# above (security audit SEV-4). Match the column list only — the VALUES clause
# is not scanned, so another table's `status` cannot be confused for this one.
CACHE_TRUTH_INSERT = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+games\s*\("
    r"[^)\";]*?"
    r"\b(?:status|status_measured_at)\b",
    re.IGNORECASE,
)


def strip_comments(source: str) -> str:
    """Blank out `#` comments, leaving every other byte (and all line numbers) in place.

    Tokenizing rather than regexing is the point: a `#` inside a string literal
    is not a comment, and an apostrophe inside a comment is not a quote. Source
    that does not lex (a partial snippet) is returned unchanged — the guard then
    scans a little less, but never crashes the build for the wrong reason.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = source.splitlines(keepends=True)
    for tok in reversed(tokens):  # reversed: earlier offsets stay valid
        if tok.type == tokenize.COMMENT:
            (row, start_col), (_, end_col) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:start_col] + line[end_col:]
    return "".join(lines)


def normalise(source: str) -> str:
    """Join implicitly concatenated string literals so one SQL statement is one span."""
    return _LITERAL_SEAM.sub("", strip_comments(source))


def writes_cache_truth(source: str) -> bool:
    """True if ``source`` contains a statement assigning games.status(_measured_at)."""
    text = normalise(source)
    return CACHE_TRUTH_WRITE.search(text) is not None or CACHE_TRUTH_INSERT.search(text) is not None


def test_only_measurement_module_writes_cache_truth() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path in ALLOWED:
            continue
        if writes_cache_truth(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        "These modules write games.status directly. Route them through "
        "orchestrator.jobs.measurement.record_measurement() (cache truth) or "
        "record_job_outcome() (how a job ended) instead: " + ", ".join(offenders)
    )


_MUST_MATCH = {
    "single-literal failed write": (
        "await deps.pool.execute_write(\n"
        "    \"UPDATE games SET status='failed', last_error=? WHERE id=?\",\n"
        "    (last_error, game_id),\n"
        ")"
    ),
    "validate's truth write": (
        '"UPDATE games SET status=?, last_validated_at=CURRENT_TIMESTAMP WHERE id=?"'
    ),
    "measurement's own write": (
        '"UPDATE games SET status=?, status_measured_at=CURRENT_TIMESTAMP, "\n'
        '"last_measure_attempt_at=CURRENT_TIMESTAMP WHERE id=?"'
    ),
    "two-literal upsert reset": (
        "\"INSERT INTO games (platform, app_id, title) VALUES ('steam', ?, ?) \"\n"
        '"ON CONFLICT(platform, app_id) DO UPDATE SET title = excluded.title, owned = 1, "\n'
        "\"status = CASE WHEN games.status = 'not_downloaded' THEN 'unknown' "
        'ELSE games.status END"'
    ),
    "statement split across lines": ('"""UPDATE games\n   SET status = \'unknown\'\n"""'),
    "guarded downloading reset": (
        "\"UPDATE games SET status='failed', last_error=? \"\n"
        "\"WHERE id=? AND status='downloading'\""
    ),
    # The truth column need not come first: an inline SQL literal ahead of it
    # must not hide it (the deleted prefill write, columns swapped).
    "truth column after a quoted value": (
        "\"UPDATE games SET last_error='boom', status='failed' WHERE id=?\""
    ),
    "truth column after a quoted version": (
        "\"UPDATE games SET cached_version='42', status='unknown' WHERE id=?\""
    ),
    # This repo's dominant multi-line SQL idiom interleaves `#` comments between
    # the literals (scheduler/jobs.py, library_sync.py, prefill.py all do it).
    "literals separated by a comment": (
        "_SQL = (\n"
        '    "UPDATE games SET owned = 1, "\n'
        "    # Keep a known-good status if this enumeration didn't carry one.\n"
        "    \"status = 'unknown' \"\n"
        '    "WHERE id=?"\n'
        ")"
    ),
    # Security audit SEV-4: an INSERT needs no UPDATE anywhere to write cache
    # truth for a new row, and library_sync already inserts into `games` — adding
    # `status` to one of those column lists was invisible to the first pattern.
    "insert with a status column": (
        '"INSERT INTO games (platform, app_id, title, status) "\n"VALUES (?, ?, ?, \'up_to_date\')"'
    ),
    "insert or replace with a status column": (
        '"INSERT OR REPLACE INTO games (id, status) VALUES (?, ?)"'
    ),
}

_MUST_NOT_MATCH = {
    "reaper's job-outcome stamp": (
        '"UPDATE games SET last_job_outcome=?, last_job_outcome_at=CURRENT_TIMESTAMP "\n'
        "\"WHERE status='downloading'\""
    ),
    "jobs table": "\"UPDATE jobs SET state='failed', finished_at=CURRENT_TIMESTAMP WHERE id=?\"",
    "platforms table": "\"UPDATE platforms SET auth_status='expired' WHERE name=?\"",
    "unrelated upsert": (
        '"INSERT INTO steam_app_info (app_id, name) VALUES (?, ?) "\n'
        '"ON CONFLICT(app_id) DO UPDATE SET name = excluded.name"'
    ),
    "attempt-only write": (
        '"UPDATE games SET last_measure_attempt_at=CURRENT_TIMESTAMP WHERE id=?"'
    ),
    "size write": '"UPDATE games SET size_bytes=? WHERE id=?"',
    # library_sync's real insert: it enumerates ownership, never cache truth.
    # Flagging it would make the guard un-greenable and get it deleted.
    "library_sync's ownership insert": (
        "\"INSERT INTO games (platform, app_id, title) VALUES ('steam', ?, ?)\""
    ),
    # Another table's `outcome`/`error` columns are not games.status.
    "validation_history insert": (
        '"INSERT INTO validation_history (game_id, method, outcome, error) "\n'
        "\"VALUES (?, 'disk_stat', ?, ?)\""
    ),
    # `state` on jobs, and a `status` word nowhere in sight.
    "jobs insert-select": (
        '"INSERT INTO jobs (kind, game_id, platform, state, source) "\n'
        "\"SELECT 'validate', id, platform, 'queued', 'sweep' FROM games\""
    ),
}


def test_pattern_catches_the_forms_it_must_and_spares_the_ones_it_must_not() -> None:
    """The scan is only as good as its regex — pin both edges of it."""
    missed = [name for name, sample in _MUST_MATCH.items() if not writes_cache_truth(sample)]
    assert not missed, f"pattern failed to catch cache-truth writes: {missed}"

    false_positives = [
        name for name, sample in _MUST_NOT_MATCH.items() if writes_cache_truth(sample)
    ]
    assert not false_positives, f"pattern flagged legitimate writes: {false_positives}"
