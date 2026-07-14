"""
Tests run against real local Postgres.
"""

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.notifications.preferences_store import (
    NotificationPreferences,
    NotificationPreferencesStore,
)

ACCOUNT_ID = "test_prefs_account"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM notification_preferences WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
        session.commit()
        session.close()


def test_get_returns_defaults_when_no_row_exists(db):
    prefs = NotificationPreferencesStore(db).get(ACCOUNT_ID)

    assert prefs.account_id == ACCOUNT_ID
    assert prefs.email_enabled is False
    assert prefs.webhook_enabled is False
    assert prefs.notify_on_kill_switch is True
    assert prefs.notify_on_credential_validation_failed is True
    assert prefs.notify_on_drawdown_breach is True
    assert prefs.updated_at is None


def test_upsert_creates_a_row_and_get_reads_it_back(db):
    store = NotificationPreferencesStore(db)
    store.upsert(
        NotificationPreferences(
            account_id=ACCOUNT_ID,
            email_enabled=True,
            email_address="ops@example.com",
            notify_on_drawdown_breach=False,
        )
    )

    prefs = store.get(ACCOUNT_ID)

    assert prefs.email_enabled is True
    assert prefs.email_address == "ops@example.com"
    assert prefs.notify_on_drawdown_breach is False
    assert prefs.updated_at is not None


def test_upsert_is_idempotent_and_overwrites_prior_values(db):
    store = NotificationPreferencesStore(db)
    store.upsert(NotificationPreferences(account_id=ACCOUNT_ID, email_enabled=True))
    store.upsert(NotificationPreferences(account_id=ACCOUNT_ID, email_enabled=False))

    prefs = store.get(ACCOUNT_ID)

    assert prefs.email_enabled is False

    row_count = db.execute(
        text("SELECT COUNT(*) FROM notification_preferences WHERE account_id = :a"),
        {"a": ACCOUNT_ID},
    ).scalar_one()
    assert row_count == 1
