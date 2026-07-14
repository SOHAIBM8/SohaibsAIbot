"""
Tests run against real local Postgres, not mocks. Each test pins its
own far-future usage_date via FrozenTracker (rather than touching
today's real row) and cleans up after itself.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.ai_assistant.events import LLMUsageCapReached
from core.ai_assistant.llm_usage_tracker import LLMUsageTracker
from core.db import SessionLocal


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


class FrozenTracker(LLMUsageTracker):
    """Pins `_today()` so every call in a test shares one usage_date
    without depending on wall-clock timing or touching today's real
    production row."""

    def __init__(self, *args, pinned_date: date, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_date = pinned_date

    def _today(self) -> date:
        return self._pinned_date


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM llm_usage_daily WHERE usage_date >= '2999-01-01'"))
        session.commit()
        session.close()


def fixed_date(offset_days: int = 0) -> date:
    return date(2999, 1, 1) + timedelta(days=offset_days)


def test_first_call_of_the_day_creates_a_row_and_succeeds(db):
    tracker = FrozenTracker(daily_cap_calls=100, db_session=db, pinned_date=fixed_date(0))
    assert tracker.check_and_increment() is True

    snapshot = tracker.snapshot()
    assert snapshot.calls_made == 1
    assert snapshot.daily_cap_reached is False


def test_cap_reached_mid_day_refuses_subsequent_calls_without_incrementing(db):
    tracker = FrozenTracker(daily_cap_calls=2, db_session=db, pinned_date=fixed_date(1))

    assert tracker.check_and_increment() is True
    assert tracker.check_and_increment() is True
    # cap is now reached (2 calls made == cap of 2)
    assert tracker.check_and_increment() is False
    assert tracker.check_and_increment() is False

    snapshot = tracker.snapshot()
    assert snapshot.calls_made == 2  # never exceeded 2, refusals didn't increment
    assert snapshot.daily_cap_reached is True


def test_cap_reached_publishes_event_exactly_once(db):
    event_bus = FakeEventBus()
    tracker = FrozenTracker(
        daily_cap_calls=1, db_session=db, pinned_date=fixed_date(2), event_bus=event_bus
    )

    tracker.check_and_increment()  # flips cap_reached true -> publishes
    tracker.check_and_increment()  # already reached -> refused, no re-publish
    tracker.check_and_increment()  # still refused, no re-publish

    assert len(event_bus.published) == 1
    assert isinstance(event_bus.published[0], LLMUsageCapReached)
    assert event_bus.published[0].date == fixed_date(2)


def test_cap_resets_at_the_next_usage_date(db):
    day1 = FrozenTracker(daily_cap_calls=1, db_session=db, pinned_date=fixed_date(3))
    day2 = FrozenTracker(daily_cap_calls=1, db_session=db, pinned_date=fixed_date(4))

    assert day1.check_and_increment() is True
    assert day1.check_and_increment() is False  # day 1 capped

    # a brand new usage_date row, independent of day 1's cap state
    assert day2.check_and_increment() is True


def test_record_usage_accumulates_tokens_and_cost(db):
    tracker = FrozenTracker(daily_cap_calls=100, db_session=db, pinned_date=fixed_date(5))
    tracker.check_and_increment()
    tracker.record_usage(tokens_used=500, cost_estimate=0.05)
    tracker.record_usage(tokens_used=300, cost_estimate=0.02)

    snapshot = tracker.snapshot()
    assert snapshot.tokens_used == 800
    assert snapshot.estimated_cost == pytest.approx(0.07)
