"""
Engine/session setup for the data store.

Defaults to a local SQLite file so the ingestion connectors and agents
can be developed and tested without standing up Postgres first. Swap
DATABASE_URL (env var) to a Postgres DSN when this moves off a single
machine — nothing in models.py or the rest of the app needs to change.
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from store.models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./trading_scanner.db")

# check_same_thread only matters for SQLite; harmless to pass otherwise-ignored
# kwargs, but keep it conditional for clarity.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables if they don't exist yet. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)


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
