"""
Shared fixtures for ingestion tests. All DB-touching tests run against
the real local Postgres (docker compose up -d + schema.sql applied),
not mocks — consistent with how the Experiment Tracker was tested
(spec section 7). Every test truncates the ingestion tables it touches
so the suite can be re-run against the same database without
accumulating state between runs.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.ingestion.types import RawCandle

INGESTION_TABLES = [
    "data_quality_report",
    "ingestion_gap",
    "tracked_instruments",
    "ingestion_watermark",
    "raw_ohlcv",
    "ingestion_run_log",
]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text(f"TRUNCATE TABLE {', '.join(INGESTION_TABLES)} RESTART IDENTITY CASCADE")
        )
        session.commit()
        session.close()


def make_candle(
    open_time: datetime, price: float = 100.0, volume: float = 10.0, interval_seconds: int = 3600
) -> RawCandle:
    return RawCandle(
        open_time=open_time,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price + 0.5,
        volume=volume,
        close_time=open_time + timedelta(seconds=interval_seconds) - timedelta(seconds=1),
        is_closed=True,
    )


def hourly_candles(start: datetime, count: int, price: float = 100.0) -> list[RawCandle]:
    return [make_candle(start + timedelta(hours=i), price=price + i) for i in range(count)]


@pytest.fixture
def now() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
