"""Packaged SQLite migrations.

Loaded at runtime via ``importlib.resources.files("orchestrator.db.migrations")``.
Each ``NNNN_name.sql`` file has a pinned SHA-256 in ``CHECKSUMS`` that must
match the file contents byte-for-byte.

Regenerate the CHECKSUMS manifest after authoring a new migration:
    python -m orchestrator.db.migrate_tools regenerate-checksums
"""

from __future__ import annotations
