"""
FastAPI dependency yielding a request-scoped SQLAlchemy session, bound
to the SAME `core.db.SessionLocal` every other component in this
project uses — the API layer is a thin wrapper over `core/`, never a
second connection path or a second place trading state could diverge
(spec's own framing: "never a second implementation of trading logic").
"""

from collections.abc import Generator

from sqlalchemy.orm import Session

from core.db import SessionLocal


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
