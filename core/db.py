"""
Database connection layer. Single source of truth for how the app
talks to Postgres — SQLAlchemy engine + session factory, configured
from the DATABASE_URL environment variable. No other module should
construct a connection string or import psycopg2 directly.
"""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://trading:trading_dev_password@localhost:5432/trading_platform",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Generator[Session, None, None]:
    """Yield a session, guaranteeing it's closed even on error."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
