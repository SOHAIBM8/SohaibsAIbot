"""
Tests run against real local Postgres. Orders/fills are seeded via
direct SQL (same pattern as test_account_resolver.py/
test_risk_decision_log_reader.py) — OrderReader is a pure reader, so
it doesn't need a real OrderManager submission path to prove its own
SQL mapping and filtering are correct.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.order_reader import OrderReader

ACCOUNT_ID = "test_reader_account"
OTHER_ACCOUNT_ID = "test_reader_other_account"
STRATEGY_ID = "test_reader_strategy"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text(
                "DELETE FROM fills WHERE client_order_id IN "
                "(SELECT client_order_id FROM orders WHERE strategy_id = :s)"
            ),
            {"s": STRATEGY_ID},
        )
        session.execute(text("DELETE FROM orders WHERE strategy_id = :s"), {"s": STRATEGY_ID})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.execute(
            text("DELETE FROM paper_accounts WHERE account_id IN (:a, :o)"),
            {"a": ACCOUNT_ID, "o": OTHER_ACCOUNT_ID},
        )
        session.commit()
        session.close()


def _seed_account(db, account_id):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": account_id, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )


def _seed_order(db, client_order_id, account_id, **overrides):
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    defaults = dict(
        exchange_order_id=None,
        symbol="BTC/USDT",
        order_type="market",
        direction=1,
        quantity=1.0,
        limit_price=None,
        stop_price=None,
        mode="paper",
        state="filled",
    )
    defaults.update(overrides)
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, exchange_order_id, strategy_id, symbol, order_type, direction,
                 quantity, limit_price, stop_price, mode, state, risk_decision_id,
                 created_at, updated_at, account_id)
            VALUES
                (:client_order_id, :exchange_order_id, :strategy_id, :symbol, :order_type,
                 :direction, :quantity, :limit_price, :stop_price, :mode, :state, :decision_id,
                 :t, :t, :account_id)
            """),
        {
            "client_order_id": client_order_id,
            "strategy_id": STRATEGY_ID,
            "decision_id": decision_id,
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "account_id": account_id,
            **defaults,
        },
    )


def _seed_fill(db, client_order_id, **overrides):
    defaults = dict(fill_price=100.0, quantity=1.0, fee=0.1, is_partial=False)
    defaults.update(overrides)
    db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES (:client_order_id, :fill_price, :quantity, :fee, :is_partial, :t)
            """),
        {"client_order_id": client_order_id, "t": datetime(2024, 1, 1, tzinfo=UTC), **defaults},
    )


def test_list_orders_scopes_to_the_requesting_account(db):
    _seed_account(db, ACCOUNT_ID)
    _seed_account(db, OTHER_ACCOUNT_ID)
    _seed_order(db, "test_co_1", ACCOUNT_ID)
    _seed_order(db, "test_co_2", OTHER_ACCOUNT_ID)
    db.commit()

    results = OrderReader(db).list_orders(account_id=ACCOUNT_ID)

    ids = [o.client_order_id for o in results]
    assert "test_co_1" in ids
    assert "test_co_2" not in ids


def test_list_orders_filters_by_symbol_and_state(db):
    _seed_account(db, ACCOUNT_ID)
    _seed_order(db, "test_co_btc", ACCOUNT_ID, symbol="BTC/USDT", state="filled")
    _seed_order(db, "test_co_eth", ACCOUNT_ID, symbol="ETH/USDT", state="rejected")
    db.commit()

    btc_only = OrderReader(db).list_orders(account_id=ACCOUNT_ID, symbol="BTC/USDT")
    rejected_only = OrderReader(db).list_orders(account_id=ACCOUNT_ID, state="rejected")

    assert [o.client_order_id for o in btc_only] == ["test_co_btc"]
    assert [o.client_order_id for o in rejected_only] == ["test_co_eth"]


def test_get_order_returns_full_record(db):
    _seed_account(db, ACCOUNT_ID)
    _seed_order(db, "test_co_detail", ACCOUNT_ID, quantity=2.5)
    db.commit()

    record = OrderReader(db).get_order("test_co_detail", account_id=ACCOUNT_ID)

    assert record is not None
    assert record.quantity == 2.5
    assert record.strategy_id == STRATEGY_ID


def test_get_order_returns_none_for_wrong_account():
    session = SessionLocal()
    try:
        _seed_account(session, ACCOUNT_ID)
        _seed_order(session, "test_co_owned", ACCOUNT_ID)
        session.commit()

        record = OrderReader(session).get_order("test_co_owned", account_id=OTHER_ACCOUNT_ID)

        assert record is None
    finally:
        session.rollback()
        session.execute(text("DELETE FROM orders WHERE client_order_id = 'test_co_owned'"))
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.commit()
        session.close()


def test_get_order_returns_none_for_unknown_id(db):
    record = OrderReader(db).get_order("no-such-order", account_id=ACCOUNT_ID)
    assert record is None


def test_list_fills_returns_fills_for_the_order(db):
    _seed_account(db, ACCOUNT_ID)
    _seed_order(db, "test_co_fills", ACCOUNT_ID)
    _seed_fill(db, "test_co_fills", fill_price=101.0)
    db.commit()

    fills = OrderReader(db).list_fills("test_co_fills")

    assert len(fills) == 1
    assert fills[0].fill_price == 101.0


def test_list_orders_respects_limit(db):
    _seed_account(db, ACCOUNT_ID)
    for i in range(3):
        _seed_order(db, f"test_co_limit_{i}", ACCOUNT_ID)
    db.commit()

    results = OrderReader(db).list_orders(account_id=ACCOUNT_ID, limit=2)

    assert len(results) == 2
