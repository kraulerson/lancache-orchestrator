"""F11: db migrate / vacuum (local, in-process)."""

from __future__ import annotations

from click.testing import CliRunner

from orchestrator.cli.main import cli


def test_db_migrate_applies(tmp_path, monkeypatch):
    db = tmp_path / "orch.db"
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(db))
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["db", "migrate"])
    get_settings.cache_clear()
    assert r.exit_code == 0, r.output
    assert db.exists()
    assert "migrat" in r.output.lower()


def test_db_vacuum_runs(tmp_path, monkeypatch):
    db = tmp_path / "orch.db"
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(db))
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    CliRunner().invoke(cli, ["db", "migrate"])  # create + migrate first
    r = CliRunner().invoke(cli, ["db", "vacuum"])
    get_settings.cache_clear()
    assert r.exit_code == 0, r.output
    assert "vacuum" in r.output.lower()


def test_db_migrate_open_failure_exits_1_cleanly(tmp_path, monkeypatch):
    """A MigrationError (e.g. an unopenable DB path) must surface as a clean
    exit 1 with a ✗ message, NOT a raw Python traceback (F11 error contract)."""
    bad = tmp_path / "nonexistent-dir" / "orch.db"  # parent dir missing
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(bad))
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["db", "migrate"])
    get_settings.cache_clear()
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)  # handled, not an escaped MigrationError
    assert "✗" in r.stderr


def test_db_vacuum_on_non_sqlite_file_exits_1_cleanly(tmp_path, monkeypatch):
    """A sqlite error (file is not a database) must surface as a clean exit 1,
    not a raw traceback."""
    notdb = tmp_path / "garbage.db"
    notdb.write_text("this is not a sqlite database")
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(notdb))
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["db", "vacuum"])
    get_settings.cache_clear()
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)
    assert "✗" in r.stderr


def test_db_vacuum_error_names_the_path(tmp_path, monkeypatch):
    """db vacuum errors must name the offending DB path (like db migrate), not
    just the bare sqlite message (UAT-11 S11-E-08)."""
    notdb = tmp_path / "garbage.db"
    notdb.write_text("this is not a sqlite database")
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(notdb))
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["db", "vacuum"])
    get_settings.cache_clear()
    assert r.exit_code == 1
    assert str(notdb) in r.stderr
