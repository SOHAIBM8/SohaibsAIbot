"""
Tests run against real local Postgres.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.notifications.notification_log import NotificationLogStore

_MARKER = "test_notification_log_marker"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM notification_log WHERE message LIKE :m"), {"m": f"{_MARKER}%"}
        )
        session.commit()
        session.close()


def test_record_and_list_recent_round_trips(db):
    store = NotificationLogStore(db)
    store.record(
        event_type="KillSwitchEngaged",
        severity="critical",
        message=f"{_MARKER} halt",
        payload={"reason": "test halt"},
        occurred_at=datetime(2999, 1, 1, tzinfo=UTC),
    )

    results = store.list_recent(limit=10)
    matching = [r for r in results if r.message.startswith(_MARKER)]

    assert len(matching) == 1
    assert matching[0].event_type == "KillSwitchEngaged"
    assert matching[0].severity == "critical"
    assert matching[0].payload == {"reason": "test halt"}


def test_list_recent_orders_most_recent_first(db):
    store = NotificationLogStore(db)
    store.record(
        event_type="CircuitBreakerTripped",
        severity="warning",
        message=f"{_MARKER} first",
        payload={},
        occurred_at=datetime(2999, 1, 1, tzinfo=UTC),
    )
    store.record(
        event_type="CircuitBreakerTripped",
        severity="warning",
        message=f"{_MARKER} second",
        payload={},
        occurred_at=datetime(2999, 1, 2, tzinfo=UTC),
    )

    results = [r for r in store.list_recent(limit=10) if r.message.startswith(_MARKER)]

    assert [r.message for r in results] == [f"{_MARKER} second", f"{_MARKER} first"]


def test_list_recent_filters_by_severity(db):
    store = NotificationLogStore(db)
    store.record(
        event_type="KillSwitchEngaged",
        severity="critical",
        message=f"{_MARKER} critical one",
        payload={},
        occurred_at=datetime(2999, 1, 1, tzinfo=UTC),
    )
    store.record(
        event_type="CircuitBreakerCleared",
        severity="info",
        message=f"{_MARKER} info one",
        payload={},
        occurred_at=datetime(2999, 1, 1, tzinfo=UTC),
    )

    critical_only = [
        r for r in store.list_recent(limit=10, severity="critical") if r.message.startswith(_MARKER)
    ]

    assert len(critical_only) == 1
    assert critical_only[0].severity == "critical"


def test_list_recent_respects_limit(db):
    store = NotificationLogStore(db)
    for i in range(3):
        store.record(
            event_type="CircuitBreakerCleared",
            severity="info",
            message=f"{_MARKER} {i}",
            payload={},
            occurred_at=datetime(2999, 1, 1, tzinfo=UTC),
        )

    results = store.list_recent(limit=2)

    assert len(results) == 2
