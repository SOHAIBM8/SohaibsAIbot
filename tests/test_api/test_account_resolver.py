"""
Tests run against real local Postgres — resolving account_id via a
real `orders` row lookup, not a mock.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from api.websocket.account_resolver import OrderAccountResolver
from core.db import SessionLocal

STRATEGY_ID = "test_resolver_strategy"
ACCOUNT_ID = "test_resolver_account"
ORDER_ID = "test_co_resolver_1"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": ORDER_ID})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.commit()
        session.close()


@pytest.fixture
def seeded_order(db):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 6, 1, tzinfo=UTC)},
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                (:o, :s, 'BTC/USDT', 'market', 1, 1.0, 'paper', 'filled', :d, :t, :t, :a)
            """),
        {
            "o": ORDER_ID,
            "s": STRATEGY_ID,
            "d": decision_id,
            "t": datetime(2024, 6, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    db.commit()


def test_resolves_account_id_from_a_direct_field():
    resolver = OrderAccountResolver(SessionLocal)
    result = resolver.resolve({"account_id": "direct_account"})
    assert result == "direct_account"


def test_resolves_account_id_via_client_order_id_lookup(seeded_order):
    resolver = OrderAccountResolver(SessionLocal)
    result = resolver.resolve({"client_order_id": ORDER_ID})
    assert result == ACCOUNT_ID


def test_returns_none_for_an_unknown_order():
    resolver = OrderAccountResolver(SessionLocal)
    result = resolver.resolve({"client_order_id": "no-such-order"})
    assert result is None


def test_returns_none_for_a_payload_with_neither_field():
    resolver = OrderAccountResolver(SessionLocal)
    result = resolver.resolve({"some_other_field": "value"})
    assert result is None


def test_direct_account_id_field_takes_priority_over_lookup(seeded_order):
    resolver = OrderAccountResolver(SessionLocal)
    result = resolver.resolve({"account_id": "explicit", "client_order_id": ORDER_ID})
    assert result == "explicit"
