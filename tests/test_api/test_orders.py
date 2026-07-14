"""
Orders/Positions API integration tests against real local Postgres.
Seeds orders/fills via direct SQL (same pattern as
tests/test_execution/test_order_reader.py) under the dashboard's test
account_id ("test_dashboard_account", set by conftest's _dashboard_env
fixture) so the authenticated session's own account_id matches what
was seeded.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

ACCOUNT_ID = "test_dashboard_account"
STRATEGY_ID = "test_api_orders_strategy"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return response.cookies["dashboard_csrf"]


@pytest.fixture
def seeded_order(db):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                ('test_api_co_1', :s, 'BTC/USDT', 'market', 1, 1.0, 'paper', 'filled',
                 :d, :t, :t, :a)
            """),
        {
            "s": STRATEGY_ID,
            "d": decision_id,
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES ('test_api_co_1', 100.0, 1.0, 0.1, FALSE, :t)
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.commit()
    yield "test_api_co_1"
    db.execute(text("DELETE FROM fills WHERE client_order_id = 'test_api_co_1'"))
    db.execute(text("DELETE FROM orders WHERE client_order_id = 'test_api_co_1'"))
    db.execute(text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
    db.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.commit()


def test_list_orders_requires_auth(client):
    response = client.get("/api/orders")
    assert response.status_code == 401


def test_list_orders_returns_seeded_order(client, seeded_order):
    _logged_in(client)
    response = client.get("/api/orders")
    assert response.status_code == 200
    ids = [row["client_order_id"] for row in response.json()]
    assert seeded_order in ids


def test_list_orders_filters_by_symbol(client, seeded_order):
    _logged_in(client)
    response = client.get("/api/orders", params={"symbol": "ETH/USDT"})
    assert response.status_code == 200
    ids = [row["client_order_id"] for row in response.json()]
    assert seeded_order not in ids


def test_get_order_returns_order_with_fills(client, seeded_order):
    _logged_in(client)
    response = client.get(f"/api/orders/{seeded_order}")
    assert response.status_code == 200
    body = response.json()
    assert body["client_order_id"] == seeded_order
    assert len(body["fills"]) == 1
    assert body["fills"][0]["fill_price"] == 100.0


def test_get_order_unknown_id_is_404(client):
    _logged_in(client)
    response = client.get("/api/orders/no-such-order")
    assert response.status_code == 404


def test_positions_reports_unavailable(client):
    _logged_in(client)
    response = client.get("/api/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["positions"] == []
    assert body["reason"]


def test_positions_requires_auth(client):
    response = client.get("/api/positions")
    assert response.status_code == 401


@pytest.fixture
def seeded_cancellable_order(db):
    order_id = "test_api_co_cancellable"
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                (:o, :s, 'BTC/USDT', 'market', 1, 1.0, 'paper', 'submitted',
                 :d, :t, :t, :a)
            """),
        {
            "o": order_id,
            "s": STRATEGY_ID,
            "d": decision_id,
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    db.commit()
    yield order_id
    db.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": order_id})
    db.execute(text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
    db.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.commit()


def test_cancel_order_requires_auth(client):
    response = client.post("/api/orders/no-such-order/cancel")
    assert response.status_code == 401


def test_cancel_order_requires_csrf(client, seeded_cancellable_order):
    _logged_in(client)
    response = client.post(f"/api/orders/{seeded_cancellable_order}/cancel")
    assert response.status_code == 403


def test_cancel_order_unknown_id_is_404(client):
    csrf_token = _logged_in(client)
    response = client.post("/api/orders/no-such-order/cancel", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 404


def test_cancel_order_transitions_to_cancelled(client, seeded_cancellable_order):
    csrf_token = _logged_in(client)

    response = client.post(
        f"/api/orders/{seeded_cancellable_order}/cancel", headers={"X-CSRF-Token": csrf_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["client_order_id"] == seeded_cancellable_order
    assert body["state"] == "cancelled"

    follow_up = client.get(f"/api/orders/{seeded_cancellable_order}")
    assert follow_up.json()["state"] == "cancelled"


def test_cancel_an_already_filled_order_is_409(client, seeded_order):
    csrf_token = _logged_in(client)

    response = client.post(
        f"/api/orders/{seeded_order}/cancel", headers={"X-CSRF-Token": csrf_token}
    )

    assert response.status_code == 409


def test_cancel_a_live_order_is_400(client, db):
    order_id = "test_api_co_live"
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                (:o, :s, 'BTC/USDT', 'market', 1, 1.0, 'live', 'submitted',
                 :d, :t, :t, :a)
            """),
        {
            "o": order_id,
            "s": STRATEGY_ID,
            "d": decision_id,
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    db.commit()
    try:
        csrf_token = _logged_in(client)
        response = client.post(
            f"/api/orders/{order_id}/cancel", headers={"X-CSRF-Token": csrf_token}
        )
        assert response.status_code == 400
    finally:
        db.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": order_id})
        db.execute(text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
        db.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        db.commit()
