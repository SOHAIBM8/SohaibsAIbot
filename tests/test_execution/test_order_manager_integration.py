"""
OrderManager against a real PaperExecutionAdapter and real Postgres —
not mocked. Every order must originate from an approved SizingDecision
(spec decision #4); a SizingDecision with approved_quantity <= 0 must
never silently produce an order.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.db import SessionLocal
from core.execution.latency_simulator import LatencySimulator
from core.execution.order import OrderState, OrderType
from core.execution.order_manager import OrderManager, TradingNotPermittedError
from core.execution.paper_execution_adapter import PaperExecutionAdapter
from core.execution_model import ExecutionModel
from core.ingestion.event_bus import PostgresEventBus
from core.risk.kill_switch import KillSwitch
from core.risk.risk_decision import SizingDecision
from core.security.arming_service import ArmingService

ACCOUNT_ID = "test_paper_account"
EXCHANGE = "binance"


class _FakeMarketDataSource:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def get_last_price(self, symbol: str) -> float:
        return self._prices[symbol]


_KILL_SWITCH_SCOPE = f"test_order_manager_{ACCOUNT_ID}"


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
                "(SELECT client_order_id FROM orders WHERE strategy_id LIKE 'test_%')"
            )
        )
        session.execute(text("DELETE FROM orders WHERE strategy_id LIKE 'test_%'"))
        session.execute(text("DELETE FROM risk_decision_log WHERE strategy_id LIKE 'test_%'"))
        session.execute(text(f"DELETE FROM paper_accounts WHERE account_id = '{ACCOUNT_ID}'"))
        session.execute(text(f"DELETE FROM live_accounts WHERE account_id = '{ACCOUNT_ID}'"))
        session.execute(text("DELETE FROM arming_state WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.execute(
            text("DELETE FROM kill_switch_state WHERE scope = :s"), {"s": _KILL_SWITCH_SCOPE}
        )
        session.commit()
        session.close()


def make_risk_decision_row(db, strategy_id="test_s1", approved_quantity=1.0) -> int:
    result = db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity)
            VALUES (:bar_time, :strategy_id, :approved_quantity, :approved_quantity)
            RETURNING id
            """),
        {
            "bar_time": datetime.now(UTC),
            "strategy_id": strategy_id,
            "approved_quantity": approved_quantity,
        },
    )
    db.commit()
    return result.scalar_one()


def make_manager(db, prices=None, starting_balance=10_000.0, mode="paper", strategy_id="test_s1"):
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=10.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource(prices or {"BTC/USDT": 100.0}),
    )
    kill_switch = None
    arming_service = None
    exchange = None
    if mode == "live":
        # A live OrderManager now structurally requires both gates
        # (docs/execution_engine_stage2_spec.md, CLAUDE.md
        # "is_trading_permitted()" gap) — a dedicated kill-switch scope
        # (never 'global') so this test can never be affected by, or
        # affect, another test's kill-switch state, and the exact
        # strategy_id under test armed for EXCHANGE so submit()'s dual
        # gate actually passes.
        kill_switch = KillSwitch(db, scope=_KILL_SWITCH_SCOPE)
        arming_service = ArmingService(db)
        exchange = EXCHANGE
        arming_service.arm(
            ACCOUNT_ID,
            strategy_id,
            EXCHANGE,
            armed_by="test_order_manager_integration",
            mainnet=False,
        )
    return OrderManager(
        execution_adapter=adapter,
        event_bus=PostgresEventBus(),
        db_session=db,
        mode=mode,
        account_id=ACCOUNT_ID,
        starting_balance=starting_balance,
        kill_switch=kill_switch,
        arming_service=arming_service,
        exchange=exchange,
    )


def test_submit_rejects_a_sizing_decision_with_zero_approved_quantity(db):
    manager = make_manager(db)
    risk_decision_id = make_risk_decision_row(db)
    decision = SizingDecision(
        approved_quantity=0.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    with pytest.raises(ValueError, match="approved_quantity"):
        manager.submit(
            decision,
            strategy_id="test_s1",
            symbol="BTC/USDT",
            order_type=OrderType.MARKET,
            direction=1,
        )

    count = db.execute(
        text("SELECT count(*) FROM orders WHERE strategy_id = 'test_s1'")
    ).scalar_one()
    assert count == 0  # never silently no-ops into a phantom order


def test_submit_rejects_a_sizing_decision_with_negative_approved_quantity(db):
    manager = make_manager(db)
    risk_decision_id = make_risk_decision_row(db)
    decision = SizingDecision(
        approved_quantity=-5.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    with pytest.raises(ValueError, match="approved_quantity"):
        manager.submit(
            decision,
            strategy_id="test_s1",
            symbol="BTC/USDT",
            order_type=OrderType.MARKET,
            direction=1,
        )


def test_submit_rejects_a_sizing_decision_with_no_risk_decision_id(db):
    manager = make_manager(db)
    decision = SizingDecision(approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=None)

    with pytest.raises(ValueError, match="risk_decision_id"):
        manager.submit(
            decision,
            strategy_id="test_s1",
            symbol="BTC/USDT",
            order_type=OrderType.MARKET,
            direction=1,
        )


def test_full_lifecycle_market_order_submit_to_filled(db):
    manager = make_manager(db, prices={"BTC/USDT": 100.0})
    risk_decision_id = make_risk_decision_row(db, approved_quantity=2.0)
    decision = SizingDecision(
        approved_quantity=2.0, proposed_quantity=2.0, risk_decision_id=risk_decision_id
    )

    order = manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    assert order.state == OrderState.FILLED

    row = (
        db.execute(
            text(
                "SELECT state, quantity, risk_decision_id FROM orders WHERE client_order_id = :id"
            ),
            {"id": order.client_order_id},
        )
        .mappings()
        .first()
    )
    assert row["state"] == "filled"
    assert float(row["quantity"]) == pytest.approx(2.0)
    assert row["risk_decision_id"] == risk_decision_id

    fill_rows = (
        db.execute(
            text("SELECT fill_price, quantity, fee FROM fills WHERE client_order_id = :id"),
            {"id": order.client_order_id},
        )
        .mappings()
        .all()
    )
    assert len(fill_rows) == 1
    assert float(fill_rows[0]["fill_price"]) == pytest.approx(100.0)
    assert float(fill_rows[0]["quantity"]) == pytest.approx(2.0)


def test_account_balance_updated_correctly_after_buy_fill(db):
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, starting_balance=10_000.0)
    risk_decision_id = make_risk_decision_row(db, approved_quantity=2.0)
    decision = SizingDecision(
        approved_quantity=2.0, proposed_quantity=2.0, risk_decision_id=risk_decision_id
    )

    manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    cash = db.execute(
        text("SELECT current_cash FROM paper_accounts WHERE account_id = :id"), {"id": ACCOUNT_ID}
    ).scalar_one()
    # notional = 100 * 2 = 200; fee = 200 * 10bps = 0.2; buy reduces cash
    expected_cash = 10_000.0 - 200.0 - 0.2
    assert float(cash) == pytest.approx(expected_cash)


def test_account_balance_updated_correctly_after_sell_fill(db):
    manager = make_manager(db, prices={"BTC/USDT": 50.0}, starting_balance=10_000.0)
    risk_decision_id = make_risk_decision_row(db, approved_quantity=1.0)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=-1,
    )

    cash = db.execute(
        text("SELECT current_cash FROM paper_accounts WHERE account_id = :id"), {"id": ACCOUNT_ID}
    ).scalar_one()
    # notional = 50 * 1 = 50; fee = 50 * 10bps = 0.05; sell increases cash
    expected_cash = 10_000.0 + 50.0 - 0.05
    assert float(cash) == pytest.approx(expected_cash)


def test_live_mode_fill_updates_live_accounts_not_paper_accounts(db):
    """docs/gap_audit_report.md P0 #1: a fill for a live-mode order must
    never touch paper_accounts.current_cash — it used to, unconditionally,
    silently mixing real and simulated trading results in one ledger."""
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, starting_balance=10_000.0, mode="live")
    risk_decision_id = make_risk_decision_row(db, approved_quantity=2.0)
    decision = SizingDecision(
        approved_quantity=2.0, proposed_quantity=2.0, risk_decision_id=risk_decision_id
    )

    order = manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    assert order.mode == "live"
    assert order.state == OrderState.FILLED

    live_cash = db.execute(
        text("SELECT current_cash FROM live_accounts WHERE account_id = :id"), {"id": ACCOUNT_ID}
    ).scalar_one()
    expected_cash = 10_000.0 - 200.0 - 0.2  # same fee math as the paper buy-fill test
    assert float(live_cash) == pytest.approx(expected_cash)

    # The account was never created in paper_accounts at all — not just
    # left at its starting balance, genuinely absent.
    paper_row = db.execute(
        text("SELECT 1 FROM paper_accounts WHERE account_id = :id"), {"id": ACCOUNT_ID}
    ).scalar_one_or_none()
    assert paper_row is None


def test_paper_mode_fill_never_touches_live_accounts(db):
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, starting_balance=10_000.0, mode="paper")
    risk_decision_id = make_risk_decision_row(db, approved_quantity=1.0)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    live_row = db.execute(
        text("SELECT 1 FROM live_accounts WHERE account_id = :id"), {"id": ACCOUNT_ID}
    ).scalar_one_or_none()
    assert live_row is None


def test_cancel_a_submitted_not_yet_filled_order(db):
    manager = make_manager(db)
    risk_decision_id = make_risk_decision_row(db)

    # Bypass manager.submit()'s automatic fill-processing to get an
    # order that is genuinely still SUBMITTED — Stage 1's synchronous
    # paper fills mean there's no such window through the public
    # submit() path alone (mirrors the same limitation noted in
    # test_paper_execution_adapter.py).
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from core.execution.order import Order

    now = _dt.now(_UTC)
    order = Order(
        client_order_id="test-manual-co-1",
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
        quantity=1.0,
        limit_price=None,
        stop_price=None,
        mode="paper",
        state=OrderState.PENDING,
        risk_decision_id=risk_decision_id,
        created_at=now,
        updated_at=now,
    )
    manager._orders[order.client_order_id] = order
    manager._insert_order(order)
    order.transition_to(OrderState.SUBMITTED, now)
    manager.execution_adapter._orders[order.client_order_id] = order

    result = manager.cancel(order.client_order_id)

    assert result.state == OrderState.CANCELLED
    row_state = db.execute(
        text("SELECT state FROM orders WHERE client_order_id = :id"), {"id": order.client_order_id}
    ).scalar_one()
    assert row_state == "cancelled"


def test_cancel_a_filled_order_raises(db):
    manager = make_manager(db)
    risk_decision_id = make_risk_decision_row(db)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    order = manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )
    assert order.state == OrderState.FILLED

    with pytest.raises(ValueError, match="illegal order state transition"):
        manager.cancel(order.client_order_id)


def test_duplicate_client_order_id_insert_is_rejected_at_the_db_level(db):
    """spec decision #2: client_order_id is the sole idempotency key.
    PaperExecutionAdapter already guards this in-process (step 3); the
    orders.client_order_id PRIMARY KEY is the persistence-layer backstop
    — a second insert with the same id must never silently succeed."""
    manager = make_manager(db)
    risk_decision_id = make_risk_decision_row(db)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    order = manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    with pytest.raises(IntegrityError):
        manager._insert_order(order)
    db.rollback()


# --- KillSwitch + ArmingService dual gate on live orders (CLAUDE.md
# "What's NOT built yet": is_trading_permitted() built and tested in
# isolation but never actually called from the submission path) --------


def test_constructing_a_live_manager_without_kill_switch_raises(db):
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=10.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource({"BTC/USDT": 100.0}),
    )
    with pytest.raises(ValueError, match="kill_switch"):
        OrderManager(
            execution_adapter=adapter,
            event_bus=PostgresEventBus(),
            db_session=db,
            mode="live",
            account_id=ACCOUNT_ID,
            arming_service=ArmingService(db),
            exchange=EXCHANGE,
        )


def test_constructing_a_live_manager_without_arming_service_raises(db):
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=10.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource({"BTC/USDT": 100.0}),
    )
    with pytest.raises(ValueError, match="kill_switch"):
        OrderManager(
            execution_adapter=adapter,
            event_bus=PostgresEventBus(),
            db_session=db,
            mode="live",
            account_id=ACCOUNT_ID,
            kill_switch=KillSwitch(db, scope=_KILL_SWITCH_SCOPE),
            exchange=EXCHANGE,
        )


def test_constructing_a_live_manager_without_exchange_raises(db):
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=10.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource({"BTC/USDT": 100.0}),
    )
    with pytest.raises(ValueError, match="kill_switch"):
        OrderManager(
            execution_adapter=adapter,
            event_bus=PostgresEventBus(),
            db_session=db,
            mode="live",
            account_id=ACCOUNT_ID,
            kill_switch=KillSwitch(db, scope=_KILL_SWITCH_SCOPE),
            arming_service=ArmingService(db),
        )


def test_paper_manager_never_requires_kill_switch_or_arming_service(db):
    """Backward compatibility, explicit: mode='paper' construction must
    remain completely unaffected by the live-mode dual-gate guard."""
    manager = make_manager(db, mode="paper")
    assert manager.kill_switch is None
    assert manager.arming_service is None


def test_live_order_refused_when_kill_switch_engaged(db):
    # KillSwitch snapshots engaged/disengaged at construction time
    # (load_state() runs once in __init__, same pattern RiskEngine's
    # own kill_switch.is_engaged() check relies on elsewhere) — engage
    # BEFORE make_manager constructs its own KillSwitch, so that
    # instance's load_state() actually picks up the engaged row,
    # matching how a fresh KillSwitch is constructed per real
    # evaluation everywhere else in this codebase.
    KillSwitch(db, scope=_KILL_SWITCH_SCOPE).engage(reason="test", engaged_by="test")
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, mode="live")
    risk_decision_id = make_risk_decision_row(db, approved_quantity=1.0)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    with pytest.raises(TradingNotPermittedError, match="kill switch is engaged"):
        manager.submit(
            decision,
            strategy_id="test_s1",
            symbol="BTC/USDT",
            order_type=OrderType.MARKET,
            direction=1,
        )

    count = db.execute(
        text("SELECT count(*) FROM orders WHERE strategy_id = 'test_s1'")
    ).scalar_one()
    assert count == 0  # refused before any order row was ever written


def test_live_order_refused_when_strategy_not_armed(db):
    # make_manager arms "test_s1" (its default strategy_id) — this
    # order is for a DIFFERENT, never-armed strategy_id.
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, mode="live")
    risk_decision_id = make_risk_decision_row(db, strategy_id="test_unarmed", approved_quantity=1.0)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    with pytest.raises(TradingNotPermittedError, match="not armed"):
        manager.submit(
            decision,
            strategy_id="test_unarmed",
            symbol="BTC/USDT",
            order_type=OrderType.MARKET,
            direction=1,
        )

    count = db.execute(
        text("SELECT count(*) FROM orders WHERE strategy_id = 'test_unarmed'")
    ).scalar_one()
    assert count == 0


def test_live_order_succeeds_when_armed_and_kill_switch_clear(db):
    """The happy path: make_manager's own live-mode setup (armed
    strategy, clear kill switch) must actually let a live order
    through — the dual gate isn't just a refusal mechanism."""
    manager = make_manager(db, prices={"BTC/USDT": 100.0}, starting_balance=10_000.0, mode="live")
    risk_decision_id = make_risk_decision_row(db, approved_quantity=1.0)
    decision = SizingDecision(
        approved_quantity=1.0, proposed_quantity=1.0, risk_decision_id=risk_decision_id
    )

    order = manager.submit(
        decision,
        strategy_id="test_s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )

    assert order.state == OrderState.FILLED
