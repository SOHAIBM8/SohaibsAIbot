"""
Tests run against real local Postgres. Fixture rows are written via the
normal app role (core.db.SessionLocal) — llm_readonly cannot write, by
design (see test_readonly_role_enforcement.py) — and ContextBuilder
itself reads them back via the llm_readonly role
(core.ai_assistant.readonly_db.ReadonlySessionLocal), exactly as it
would in production.

The account-isolation test called for in docs/ai_assistant_spec.md
section 5 ("a context built for account A never includes account B's
rows") is added in step 5 alongside build_daily_summary_context, the
only method in this class that is account-scoped — trade/risk-decision/
regime contexts (this step's scope) are keyed by order_id/decision_id/
symbol, not account_id, so there is nothing account-scoped to isolate
here yet.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal

ORDER_ID = "test_co_ctx_1"
STRATEGY_ID = "test_strategy_ctx"
SYMBOL = "BTC/USDT"


@pytest.fixture
def write_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM fills WHERE client_order_id LIKE 'test_co_ctx_%'"))
        session.execute(text("DELETE FROM orders WHERE client_order_id LIKE 'test_co_ctx_%'"))
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.execute(text("DELETE FROM signal_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
        session.commit()
        session.close()


@pytest.fixture
def readonly_db():
    session = ReadonlySessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def seeded(write_db):
    bar_time = datetime(2024, 6, 1, 11, 55, tzinfo=UTC)
    created_at = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)

    write_db.execute(
        text("""
            INSERT INTO signal_log
                (symbol, bar_time, strategy_id, regime, regime_confidence, direction,
                 signal_strength, confidence, reasons, rejected_reasons, acted_on)
            VALUES
                (:symbol, :bar_time, :strategy_id, 'trending_up', 0.8, 1,
                 0.7, 0.65, ARRAY['ema_cross_up'], ARRAY[]::TEXT[], TRUE)
            """),
        {"symbol": SYMBOL, "bar_time": bar_time, "strategy_id": STRATEGY_ID},
    )

    decision_id = write_db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity,
                 rejection_reason, throttle_reasons, layer_results)
            VALUES
                (:bar_time, :strategy_id, 1.0, 1.0, NULL, ARRAY[]::TEXT[], :layer_results)
            RETURNING id
            """),
        {
            "bar_time": bar_time,
            "strategy_id": STRATEGY_ID,
            "layer_results": (
                '[{"layer_name": "gate", "passed": true, "multiplier": 1.0, "reason": null}]'
            ),
        },
    ).scalar_one()

    write_db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at)
            VALUES
                (:order_id, :strategy_id, :symbol, 'market', 1, 1.0,
                 'paper', 'filled', :decision_id, :created_at, :created_at)
            """),
        {
            "order_id": ORDER_ID,
            "strategy_id": STRATEGY_ID,
            "symbol": SYMBOL,
            "decision_id": decision_id,
            "created_at": created_at,
        },
    )

    write_db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES (:order_id, 65000.0, 1.0, 5.0, FALSE, :filled_at)
            """),
        {"order_id": ORDER_ID, "filled_at": created_at},
    )
    write_db.commit()
    return {"decision_id": decision_id, "bar_time": bar_time, "created_at": created_at}


def test_build_trade_context_grounds_order_fills_signal_and_risk_decision(seeded, readonly_db):
    builder = ContextBuilder(readonly_db)
    ctx = builder.build_trade_context(ORDER_ID)

    assert ctx.order.client_order_id == ORDER_ID
    assert ctx.order.symbol == SYMBOL
    assert len(ctx.fills) == 1
    assert ctx.fills[0].fill_price == 65000.0
    assert ctx.signal.strategy_id == STRATEGY_ID
    assert ctx.signal.regime == "trending_up"
    assert ctx.risk_decision.id == seeded["decision_id"]
    assert ctx.regime_at_entry == "trending_up"


def test_build_trade_context_raises_for_unknown_order(readonly_db):
    builder = ContextBuilder(readonly_db)
    with pytest.raises(KeyError, match="no order"):
        builder.build_trade_context("test_co_ctx_does_not_exist")


def test_build_risk_decision_context_returns_layer_results(seeded, readonly_db):
    builder = ContextBuilder(readonly_db)
    ctx = builder.build_risk_decision_context(seeded["decision_id"])

    assert ctx.decision.id == seeded["decision_id"]
    assert len(ctx.layer_results) == 1
    assert ctx.layer_results[0].layer_name == "gate"
    assert ctx.layer_results[0].passed is True


def test_build_risk_decision_context_raises_for_unknown_id(readonly_db):
    builder = ContextBuilder(readonly_db)
    with pytest.raises(KeyError, match="no risk_decision_log row"):
        builder.build_risk_decision_context(-1)


def test_build_regime_context_returns_history_within_window(seeded, readonly_db):
    builder = ContextBuilder(readonly_db)
    start = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 2, 0, 0, tzinfo=UTC)

    ctx = builder.build_regime_context(SYMBOL, start, end)

    assert ctx.symbol == SYMBOL
    assert len(ctx.regime_history) == 1
    assert ctx.regime_history[0]["regime"] == "trending_up"


def test_build_regime_context_empty_outside_window(seeded, readonly_db):
    builder = ContextBuilder(readonly_db)
    start = datetime(2023, 1, 1, tzinfo=UTC)
    end = datetime(2023, 1, 2, tzinfo=UTC)

    ctx = builder.build_regime_context(SYMBOL, start, end)
    assert ctx.regime_history == []
