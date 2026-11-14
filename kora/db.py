"""Engine/session management.

SQLite (dev/demo): WAL + BEGIN IMMEDIATE so write transactions serialise
cleanly instead of failing on lock upgrades; foreign keys on.
PostgreSQL (production): READ COMMITTED + explicit row locks
(SELECT ... FOR UPDATE [SKIP LOCKED]) where invariants need them.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_engine: Engine | None = None
_factory: sessionmaker | None = None


def init_engine(url: str) -> Engine:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _rec):  # pragma: no cover - driver glue
            dbapi_conn.isolation_level = None   # we issue BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver glue
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        # UTF-8 on the wire whatever the server default (a SQL_ASCII server otherwise returns bytes)
        args = {"client_encoding": "utf8"} if url.startswith("postgresql") else {}
        engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, connect_args=args)
    _engine = engine
    _factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database not initialised; call kora.db.init_engine()")
    return _engine


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"


def create_all() -> None:
    if is_postgres():
        # several containers start at once: one creates the schema, the others wait for it
        with engine().begin() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(727274)")
            Base.metadata.create_all(conn)
    else:
        Base.metadata.create_all(engine())
    migrate_additive()


def migrate_additive() -> list[str]:
    """Add columns that exist in the models but not in the database (safe, additive only).
    Destructive or data-moving changes belong in Alembic migrations."""
    from sqlalchemy import inspect, text
    eng = engine()
    added = []
    with eng.begin() as conn:                    # one connection: SQLite allows a single writer
        if eng.dialect.name == "postgresql":
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(727274)")
        insp = inspect(conn)
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                ddl_type = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v) if isinstance(v, str) else v}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}'))
                added.append(f"{table.name}.{col.name}")
    return added


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    if _factory is None:
        raise RuntimeError("database not initialised")
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def new_session() -> Session:
    if _factory is None:
        raise RuntimeError("database not initialised")
    return _factory()
