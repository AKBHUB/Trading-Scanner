"""
Engine/session setup for the data store.

Defaults to a local SQLite file so the ingestion connectors and agents
can be developed and tested without standing up Postgres first. Swap
DATABASE_URL (env var) to a Postgres DSN when this moves off a single
machine — nothing in models.py or the rest of the app needs to change.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from store.models import Base

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "trading_scanner.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")

# check_same_thread only matters for SQLite; harmless to pass otherwise-ignored
# kwargs, but keep it conditional for clarity.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
# expire_on_commit=False: every agent (agents/technical.py etc.) queries
# inside `with get_session() as session:` but reads the returned row's
# attributes *after* that block exits — which commits and, by default,
# expires every object so the next attribute access re-queries using a
# now-closed session. That raised DetachedInstanceError on every agent
# run. With expire_on_commit=False, objects keep their already-loaded
# values after commit/close instead of trying to refresh from the DB.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)


def init_db() -> None:
    """Create all tables if they don't exist yet, then self-heal any table
    that exists but is missing a column the model now declares. Safe to
    call on every startup."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """
    create_all() only creates tables that don't exist — it never alters an
    already-existing table to add a newly-declared column. This project
    has no migration tool (Alembic), so a column added to a model (e.g.
    Fundamentals.metrics) would otherwise need someone to remember to run
    a manual ALTER TABLE against every already-deployed database, and
    fail loudly (UndefinedColumn) until they did.

    Instead: diff each model's declared columns against what the live
    table actually has, and ALTER TABLE ADD COLUMN for anything missing.
    This only ever adds columns — never renames, drops, or changes a
    type — so it's safe to run unconditionally on every startup. A
    failure on one column is logged and skipped rather than raised, so a
    permissions issue or an unsupported type on one column can't take
    down app startup entirely.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new table — create_all() above already made it with every column
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                try:
                    col_type = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                    logger.warning("Added missing column %s.%s (%s)", table.name, column.name, col_type)
                except Exception as e:
                    logger.error(
                        "Could not add missing column %s.%s: %s: %s", table.name, column.name, type(e).__name__, e
                    )


@contextmanager
def get_session():
    """
    Usage:
        with get_session() as session:
            session.add(some_row)
    Commits on clean exit, rolls back on exception.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
