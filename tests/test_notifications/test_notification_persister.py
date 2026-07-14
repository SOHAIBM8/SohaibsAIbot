"""
NotificationPersister tests using a fake EventBus (same established
pattern as tests/test_api/test_gateway.py's FakeEventBus) plus a real
NotificationLogStore against real local Postgres — proves the full
subscribe -> persist path without needing a real PostgresEventBus
LISTEN/NOTIFY thread.
"""

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.ingestion.event_bus import EventBus, EventHandler
from core.notifications.notification_log import NotificationLogStore
from core.notifications.notification_persister import NotificationPersister

_MARKER = "test_notification_persister_marker"


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def publish(self, event) -> None:  # pragma: no cover - unused by these tests
        raise NotImplementedError

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def fire(self, event_type: str, payload: dict) -> None:
        for handler in self._handlers.get(event_type, []):
            handler(payload)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM notification_log WHERE message LIKE :m"), {"m": f"%{_MARKER}%"}
        )
        session.commit()
        session.close()


def test_kill_switch_engaged_event_is_persisted_with_critical_severity(db):
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))
    persister.start()

    bus.fire(
        "KillSwitchEngaged",
        {
            "event_type": "KillSwitchEngaged",
            "engaged_by": "risk_engine",
            "reason": f"{_MARKER} drawdown tier 3",
            "occurred_at": "2024-01-01T00:00:00+00:00",
        },
    )

    results = [
        r
        for r in NotificationLogStore(db).list_recent(limit=50)
        if _MARKER in r.message and r.event_type == "KillSwitchEngaged"
    ]
    assert len(results) == 1
    assert results[0].severity == "critical"
    assert "risk_engine" in results[0].message


def test_circuit_breaker_cleared_is_info_severity(db):
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))
    persister.start()

    bus.fire(
        "CircuitBreakerCleared",
        {
            "event_type": "CircuitBreakerCleared",
            "breaker_name": f"{_MARKER}_breaker",
            "occurred_at": "2024-01-01T00:00:00+00:00",
        },
    )

    results = [
        r
        for r in NotificationLogStore(db).list_recent(limit=50)
        if _MARKER in r.message and r.event_type == "CircuitBreakerCleared"
    ]
    assert len(results) == 1
    assert results[0].severity == "info"


def test_bus_never_subscribes_to_an_unrelated_event_type(db):
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))
    persister.start()

    # OrderFilled is a real event type (core/execution/events.py) but
    # not in NOTIFICATION_EVENT_TYPES — start() must never subscribe to
    # it, so firing it on the bus reaches no handler at all.
    assert "OrderFilled" not in bus._handlers


def test_an_event_type_not_in_the_severity_map_is_never_persisted(db):
    """Defense in depth: even if _on_event were somehow invoked for an
    unmapped event_type (bus.fire() can't do this today since start()
    only subscribes to NOTIFICATION_EVENT_TYPES, but _on_event's own
    guard is what actually prevents a KeyError/bad insert)."""
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))

    persister._on_event({"event_type": "OrderFilled", "client_order_id": f"{_MARKER}_order"})

    results = [
        r for r in NotificationLogStore(db).list_recent(limit=50) if _MARKER in str(r.payload)
    ]
    assert results == []


def test_missing_occurred_at_falls_back_to_now(db):
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))
    persister.start()

    bus.fire(
        "ArmingExpired",
        {
            "event_type": "ArmingExpired",
            "strategy_id": f"{_MARKER}_strategy",
            "exchange": "binance",
        },
    )

    results = [
        r
        for r in NotificationLogStore(db).list_recent(limit=10)
        if f"{_MARKER}_strategy" in r.message
    ]
    assert len(results) == 1
    assert results[0].occurred_at is not None
