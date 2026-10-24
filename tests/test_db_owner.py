"""Concord Bench refuses a database it did not write, and changes nothing in it (core/db.py)."""

import sqlite3

import pytest

from concord_bench import cli
from concord_bench.core import db, paths
from concord_bench.core.settings import reset_settings


@pytest.fixture
def empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCORD_DATA", str(tmp_path / "data"))
    reset_settings()
    db.reset_engines()
    yield tmp_path / "data"
    db.reset_engines()
    reset_settings()


def _foreign_db(version: str | None, tables=("dataset", "segment")) -> None:
    path = paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    if version is not None:
        con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        con.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    con.commit()
    con.close()


def _state() -> tuple[set[str], list]:
    con = sqlite3.connect(paths.db_path())
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    version = con.execute("SELECT version_num FROM alembic_version").fetchall() if "alembic_version" in tables else []
    con.close()
    return tables, version


def _latest() -> str:
    return max(db.known_revisions())


def test_ids_are_bench_only():
    revs = db.known_revisions()
    assert revs == {f"bench_{k:04d}" for k in range(1, len(revs) + 1)}  # bench_0001 ... contiguous
    assert db.next_revision_id() == f"bench_{len(revs) + 1:04d}"


def test_a_new_folder_migrates_and_a_second_migrate_is_fine(empty_dir):
    db.migrate()
    db.migrate()
    assert _state()[1] == [(_latest(),)]


@pytest.mark.parametrize(("version", "says"), [
    ("0001", "single Concord app before the split (schema 0001)"),
    ("0003", "single Concord app before the split (schema 0003)"),
    ("label_0002", "belongs to Concord Label (schema label_0002)"),
    ("bench_0099", "newer Concord Bench (schema bench_0099)"),
    ("5f2c1a", "schema version '5f2c1a' is unknown"),
    (None, "has tables but no schema version"),
])
def test_a_foreign_database_is_refused_and_left_alone(empty_dir, version, says):
    _foreign_db(version)
    before = _state()
    with pytest.raises(db.ForeignDatabase, match="not a Concord Bench database") as err:
        db.migrate()
    assert says in str(err.value) and "has not changed it" in str(err.value)
    assert _state() == before


def test_cli_migrate_prints_the_reason_without_a_traceback(empty_dir, capsys):
    _foreign_db("0001")
    assert cli.main(["migrate"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "single Concord app" in err and "Traceback" not in err
