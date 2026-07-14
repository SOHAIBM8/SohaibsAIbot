"""
Notifications API integration tests against real local Postgres. Seeds
via the real NotificationLogStore write path.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.notifications.notification_log import NotificationLogStore
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

_MARKER = "test_api_notifications_marker"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def seeded_notifications(db):
    store = NotificationLogStore(db)
    store.record(
        event_type="KillSwitchEngaged",
        severity="critical",
        message=f"{_MARKER} critical",
        payload={"reason": "test"},
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    store.record(
        event_type="CircuitBreakerCleared",
        severity="info",
        message=f"{_MARKER} info",
        payload={},
        occurred_at=datetime(2024, 1, 2, tzinfo=UTC),
    )
    yield
    db.execute(text("DELETE FROM notification_log WHERE message LIKE :m"), {"m": f"%{_MARKER}%"})
    db.commit()


def test_list_notifications_requires_auth(client):
    response = client.get("/api/notifications")
    assert response.status_code == 401


def test_list_notifications_returns_seeded_rows_most_recent_first(client, seeded_notifications):
    _logged_in(client)

    response = client.get("/api/notifications", params={"limit": 50})

    assert response.status_code == 200
    matching = [row for row in response.json() if _MARKER in row["message"]]
    assert len(matching) == 2
    assert matching[0]["message"] == f"{_MARKER} info"
    assert matching[1]["message"] == f"{_MARKER} critical"


def test_list_notifications_filters_by_severity(client, seeded_notifications):
    _logged_in(client)

    response = client.get("/api/notifications", params={"severity": "critical", "limit": 50})

    assert response.status_code == 200
    matching = [row for row in response.json() if _MARKER in row["message"]]
    assert len(matching) == 1
    assert matching[0]["severity"] == "critical"


def test_notification_persister_is_wired_into_lifespan_when_gateway_enabled(db, monkeypatch):
    """Full lifespan startup (real PostgresEventBus.start()) must not
    crash, and app.state.notification_persister must exist — the
    fake-EventBus tests in tests/test_notifications/test_notification_persister.py
    already prove the persist logic itself; this proves it's actually
    wired into the running app, not just defined."""
    monkeypatch.setenv("DASHBOARD_ENABLE_EVENT_GATEWAY", "true")
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as test_client:
        assert test_client.app.state.notification_persister is not None
        assert test_client.app.state.event_bus is not None
