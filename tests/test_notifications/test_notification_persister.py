"""
NotificationPersister tests using a fake EventBus (same established
pattern as tests/test_api/test_gateway.py's FakeEventBus) plus a real
NotificationLogStore against real local Postgres — proves the full
subscribe -> persist path without needing a real PostgresEventBus
LISTEN/NOTIFY thread.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.ingestion.event_bus import EventBus, EventHandler
from core.notifications.email_sender import EmailSender
from core.notifications.notification_log import NotificationLogStore
from core.notifications.notification_persister import NotificationPersister
from core.notifications.preferences_store import (
    NotificationPreferences,
    NotificationPreferencesStore,
)

_MARKER = "test_notification_persister_marker"
_EMAIL_ACCOUNT_ID = "test_notification_persister_account"
_ENV_PREFIX = "TEST_NP_EMAIL_"


class _FakeSMTPClient:
    def __init__(self, host, port, raise_on_send=False):
        self.sent_messages: list = []
        self.raise_on_send = raise_on_send

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if self.raise_on_send:
            raise ConnectionError("simulated SMTP failure")
        self.sent_messages.append(msg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def make_email_sender(fake_client, monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    return EmailSender(
        host_env_var=f"{_ENV_PREFIX}HOST",
        port_env_var=f"{_ENV_PREFIX}PORT",
        username_env_var=f"{_ENV_PREFIX}USERNAME",
        password_env_var=f"{_ENV_PREFIX}PASSWORD",
        from_address_env_var=f"{_ENV_PREFIX}FROM_ADDRESS",
        smtp_client_factory=lambda host, port: fake_client,
    )


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
        session.execute(
            text("DELETE FROM notification_preferences WHERE account_id = :a"),
            {"a": _EMAIL_ACCOUNT_ID},
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
            # A real "now" timestamp, not a fixed past date: list_recent()
            # orders by occurred_at DESC LIMIT 50, and a shared local dev
            # Postgres accumulates far more than 50 notification_log rows
            # across repeated test runs — a stale hardcoded date silently
            # falls outside that window once enough newer rows exist.
            "occurred_at": datetime.now(UTC).isoformat(),
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
            "occurred_at": datetime.now(UTC).isoformat(),
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


# --- email dispatch (docs/gap_audit_report.md P0 #3) ------------------------


def test_email_sent_when_kill_switch_engaged_and_preferences_fully_enabled(db, monkeypatch):
    NotificationPreferencesStore(db).upsert(
        NotificationPreferences(
            account_id=_EMAIL_ACCOUNT_ID,
            email_enabled=True,
            email_address="ops@example.com",
            notify_on_kill_switch=True,
        )
    )
    fake_smtp = _FakeSMTPClient("smtp.example.com", 587)
    bus = FakeEventBus()
    persister = NotificationPersister(
        bus,
        store_factory=lambda: NotificationLogStore(db),
        preferences_store_factory=lambda: NotificationPreferencesStore(db),
        email_sender=make_email_sender(fake_smtp, monkeypatch),
        account_id=_EMAIL_ACCOUNT_ID,
    )
    persister.start()

    bus.fire(
        "KillSwitchEngaged",
        {
            "event_type": "KillSwitchEngaged",
            "engaged_by": "devops",
            "reason": f"{_MARKER} halt",
            "occurred_at": "2024-01-01T00:00:00+00:00",
        },
    )

    assert len(fake_smtp.sent_messages) == 1
    sent = fake_smtp.sent_messages[0]
    assert sent["To"] == "ops@example.com"
    assert "CRITICAL" in sent["Subject"]
    assert _MARKER in sent.get_content()


def test_email_not_sent_when_email_disabled(db, monkeypatch):
    NotificationPreferencesStore(db).upsert(
        NotificationPreferences(
            account_id=_EMAIL_ACCOUNT_ID,
            email_enabled=False,
            email_address="ops@example.com",
            notify_on_kill_switch=True,
        )
    )
    fake_smtp = _FakeSMTPClient("smtp.example.com", 587)
    bus = FakeEventBus()
    persister = NotificationPersister(
        bus,
        store_factory=lambda: NotificationLogStore(db),
        preferences_store_factory=lambda: NotificationPreferencesStore(db),
        email_sender=make_email_sender(fake_smtp, monkeypatch),
        account_id=_EMAIL_ACCOUNT_ID,
    )
    persister.start()

    bus.fire(
        "KillSwitchEngaged",
        {"event_type": "KillSwitchEngaged", "engaged_by": "devops", "reason": _MARKER},
    )

    assert fake_smtp.sent_messages == []


def test_email_not_sent_when_category_toggle_is_off(db, monkeypatch):
    NotificationPreferencesStore(db).upsert(
        NotificationPreferences(
            account_id=_EMAIL_ACCOUNT_ID,
            email_enabled=True,
            email_address="ops@example.com",
            notify_on_kill_switch=False,  # email is on, but not for this category
        )
    )
    fake_smtp = _FakeSMTPClient("smtp.example.com", 587)
    bus = FakeEventBus()
    persister = NotificationPersister(
        bus,
        store_factory=lambda: NotificationLogStore(db),
        preferences_store_factory=lambda: NotificationPreferencesStore(db),
        email_sender=make_email_sender(fake_smtp, monkeypatch),
        account_id=_EMAIL_ACCOUNT_ID,
    )
    persister.start()

    bus.fire(
        "KillSwitchEngaged",
        {"event_type": "KillSwitchEngaged", "engaged_by": "devops", "reason": _MARKER},
    )

    assert fake_smtp.sent_messages == []


def test_email_not_sent_for_event_type_with_no_toggle_mapping(db, monkeypatch):
    """CircuitBreakerTripped has no notification_preferences toggle —
    stays in-app-feed-only regardless of email_enabled."""
    NotificationPreferencesStore(db).upsert(
        NotificationPreferences(
            account_id=_EMAIL_ACCOUNT_ID, email_enabled=True, email_address="ops@example.com"
        )
    )
    fake_smtp = _FakeSMTPClient("smtp.example.com", 587)
    bus = FakeEventBus()
    persister = NotificationPersister(
        bus,
        store_factory=lambda: NotificationLogStore(db),
        preferences_store_factory=lambda: NotificationPreferencesStore(db),
        email_sender=make_email_sender(fake_smtp, monkeypatch),
        account_id=_EMAIL_ACCOUNT_ID,
    )
    persister.start()

    bus.fire(
        "CircuitBreakerTripped",
        {"event_type": "CircuitBreakerTripped", "breaker_name": f"{_MARKER}_b", "reason": "x"},
    )

    assert fake_smtp.sent_messages == []
    # But it's still in the in-app feed.
    results = [r for r in NotificationLogStore(db).list_recent(limit=50) if _MARKER in r.message]
    assert len(results) == 1


def test_persistence_still_happens_even_if_email_send_fails(db, monkeypatch):
    NotificationPreferencesStore(db).upsert(
        NotificationPreferences(
            account_id=_EMAIL_ACCOUNT_ID,
            email_enabled=True,
            email_address="ops@example.com",
            notify_on_kill_switch=True,
        )
    )
    fake_smtp = _FakeSMTPClient("smtp.example.com", 587, raise_on_send=True)
    bus = FakeEventBus()
    persister = NotificationPersister(
        bus,
        store_factory=lambda: NotificationLogStore(db),
        preferences_store_factory=lambda: NotificationPreferencesStore(db),
        email_sender=make_email_sender(fake_smtp, monkeypatch),
        account_id=_EMAIL_ACCOUNT_ID,
    )
    persister.start()

    bus.fire(
        "KillSwitchEngaged",
        {"event_type": "KillSwitchEngaged", "engaged_by": "devops", "reason": _MARKER},
    )

    results = [r for r in NotificationLogStore(db).list_recent(limit=50) if _MARKER in r.message]
    assert len(results) == 1  # the in-app feed write must not be a casualty of an email failure


def test_no_email_attempted_when_persister_has_no_email_sender_configured(db):
    """Backward compatibility: the default construction (no email
    wiring) never even looks at notification_preferences."""
    bus = FakeEventBus()
    persister = NotificationPersister(bus, store_factory=lambda: NotificationLogStore(db))
    persister.start()

    # Must not raise even though no preferences_store_factory/email_sender exist.
    bus.fire(
        "KillSwitchEngaged",
        {"event_type": "KillSwitchEngaged", "engaged_by": "devops", "reason": _MARKER},
    )

    results = [r for r in NotificationLogStore(db).list_recent(limit=50) if _MARKER in r.message]
    assert len(results) == 1
