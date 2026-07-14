"""
Portfolio API integration tests against real local Postgres. Uses the
dashboard's test account_id ("test_dashboard_account", set by
conftest's _dashboard_env fixture) so the authenticated session's
account_id matches what's seeded.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

ACCOUNT_ID = "test_dashboard_account"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def seeded_account(db):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 97500, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.commit()
    yield ACCOUNT_ID
    db.execute(text("DELETE FROM account_snapshots WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.commit()


def test_get_account_requires_auth(client):
    response = client.get("/api/portfolio/account")
    assert response.status_code == 401


def test_get_account_returns_404_when_no_account_row(client):
    _logged_in(client)
    response = client.get("/api/portfolio/account")
    assert response.status_code == 404


def test_get_account_returns_seeded_values(client, seeded_account):
    _logged_in(client)
    response = client.get("/api/portfolio/account")
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == ACCOUNT_ID
    assert body["current_cash"] == 97500
    assert body["starting_balance"] == 100000


def test_equity_curve_reports_unavailable_when_no_snapshots(client, seeded_account):
    _logged_in(client)
    response = client.get("/api/portfolio/equity-curve")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["snapshots"] == []
    assert body["reason"]


def test_equity_curve_returns_snapshots_when_present(client, db, seeded_account):
    db.execute(
        text("""
            INSERT INTO account_snapshots (account_id, equity, open_position_count, snapshot_at)
            VALUES (:a, 100500, 0, :t)
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 2, tzinfo=UTC)},
    )
    db.commit()
    _logged_in(client)

    response = client.get("/api/portfolio/equity-curve")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["snapshots"][0]["equity"] == 100500
