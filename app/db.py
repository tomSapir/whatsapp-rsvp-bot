"""SQLAlchemy engine, session factory, and the SQLite pragmas the app depends on.

One SQLite file is shared by **two processes** — the FastAPI engine (writes incoming
RSVPs, runs the reminder job) and the Streamlit host app (reads/writes the dashboard).
To make that safe and correct, every connection enables two pragmas:

* ``journal_mode=WAL`` — write-ahead logging lets one writer and many readers proceed
  concurrently instead of contending on a single database-file lock. This is what keeps
  the two processes from blocking each other at personal scale (PLAN §11).
* ``foreign_keys=ON`` — SQLite ships with foreign-key enforcement **off**. Without this,
  the FK/CHECK constraints declared on the M1 models would be silently ignored (and the
  constraint tests would falsely pass), so we turn it on for every connection.

The module is split so it stays testable: :func:`create_db_engine` is a pure factory
(point it at a temp SQLite in tests), while :func:`get_engine` / :func:`get_sessionmaker`
lazily build the process-wide instances from :func:`app.config.get_settings`. Importing
this module has no side effects — nothing touches the filesystem or the environment until
one of the cached accessors is first called.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models; ``app/models.py`` subclasses this.

    The init helper builds every table via ``Base.metadata.create_all(engine)``.
    """


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Apply WAL + foreign-key + synchronous pragmas to each new DBAPI connection.

    Registered as a SQLAlchemy ``connect`` event listener so it runs once per physical
    connection the pool opens. ``synchronous=NORMAL`` is the recommended durability/speed
    trade-off under WAL (safe against application crashes; the small residual risk on OS
    power-loss is irrelevant for this project).
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    """Build an :class:`Engine` for ``database_url`` with the SQLite pragmas wired in.

    ``check_same_thread=False`` is required because FastAPI may use a session from a
    different thread than the one that opened the connection; SQLAlchemy's connection
    pool keeps that safe at our scale. Pass a temp-file or in-memory URL here to get an
    isolated engine for tests.
    """
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide engine, built once from settings on first call.

    Ensures the database file's parent directory exists first, since SQLite will not
    create missing directories when it opens the file.
    """
    settings = get_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_db_engine(settings.database_url)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide session factory bound to :func:`get_engine`.

    ``expire_on_commit=False`` so objects stay usable after ``commit()`` (handy for the
    Streamlit dashboard, which reads attributes off committed rows).
    """
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def init_db(engine: Engine | None = None) -> Engine:
    """Create every table on ``engine`` (defaults to the process-wide engine).

    Importing ``app.models`` here — lazily, to avoid a circular import, since models import
    :data:`Base` from this module — registers all four tables on ``Base.metadata`` before
    ``create_all`` runs. ``create_all`` issues ``CREATE TABLE IF NOT EXISTS``, so calling
    this at app startup or from a test fixture is safe to repeat.
    """
    import app.models  # noqa: F401  -- side effect: registers tables on Base.metadata

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session scope: commit on success, roll back on error.

    Usage::

        with session_scope() as session:
            session.add(obj)
        # committed here; rolled back instead if the block raised
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
