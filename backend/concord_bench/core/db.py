"""SQLite (WAL) engine and sessions. Schema changes go through Alembic (`just migrate`).

Concord Bench's revision ids start with `bench_`, so a database written by Concord Label (`label_...`)
or by the single Concord app before the split (`0001`..`0003`) is never mistaken for one of ours.
`migrate()` refuses any database it did not write, before changing anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import paths

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
APP = "Concord Bench"
REVISION_PREFIX = "bench_"
OTHER_APPS = {"label_": "Concord Label"}
SINGLE_APP_REVISIONS = {"0001", "0002", "0003"}  # the one Concord app before the split


class ForeignDatabase(RuntimeError):
    """The database was not written by this app; nothing was changed."""


@lru_cache(maxsize=4)
def _engine(url: str) -> Engine:
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return eng


def db_url() -> str:
    return f"sqlite:///{paths.db_path()}"


def engine() -> Engine:
    return _engine(db_url())


def SessionLocal() -> Session:  # noqa: N802 - conventional name
    return sessionmaker(bind=engine(), expire_on_commit=False)()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", db_url())
    return cfg


def known_revisions() -> set[str]:
    from alembic.script import ScriptDirectory

    return {s.revision for s in ScriptDirectory.from_config(alembic_config()).walk_revisions()}


def check_owner() -> None:
    """Raise ForeignDatabase unless the database is missing, empty, or at one of this app's revisions.
    Reads only; never creates or changes the file."""
    path = paths.db_path()
    if not path.is_file():
        return
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall() if "alembic_version" in tables else []
    finally:
        con.close()
    version = rows[0][0] if rows else None
    if version is None and not tables - {"alembic_version", "sqlite_sequence"}:
        return  # a new, empty database
    if version in known_revisions():
        return
    if version is None:
        who = "it has tables but no schema version"
    elif version in SINGLE_APP_REVISIONS:
        who = f"it was written by the single Concord app before the split (schema {version})"
    elif version.startswith(REVISION_PREFIX):
        who = f"it was written by a newer {APP} (schema {version})"
    else:
        other = next((name for prefix, name in OTHER_APPS.items() if version.startswith(prefix)), None)
        who = f"it belongs to {other} (schema {version})" if other else f"its schema version {version!r} is unknown"
    raise ForeignDatabase(
        f"{path} is not a {APP} database: {who}. {APP} has not changed it. "
        f"Set CONCORD_DATA to an empty folder to start there, or open this data with the app that wrote it.")


def migrate() -> None:
    """Create the data folders and bring the database to the latest schema, after check_owner()."""
    from alembic import command

    check_owner()
    paths.ensure_dirs()
    command.upgrade(alembic_config(), "head")


def next_revision_id() -> str:
    """`bench_NNNN` for `just revision`, one past the highest existing number."""
    nums = [int(r.removeprefix(REVISION_PREFIX)) for r in known_revisions()
            if r.startswith(REVISION_PREFIX) and r.removeprefix(REVISION_PREFIX).isdigit()]
    return f"{REVISION_PREFIX}{max(nums, default=0) + 1:04d}"


def reset_engines() -> None:
    _engine.cache_clear()
