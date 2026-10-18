"""SQLite (WAL) engine and sessions. Schema changes go through Alembic (`just migrate`)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import paths

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


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


def migrate() -> None:
    """Create the data folders and bring the database to the latest schema."""
    from alembic import command

    paths.ensure_dirs()
    command.upgrade(alembic_config(), "head")


def reset_engines() -> None:
    _engine.cache_clear()
