"""
Runs against real local Postgres (orders/fills/reconciliation_log rows
are real), with a fake ExecutionAdapter returning scripted exchange
states — no real network, exactly like every other exchange-facing
component's tests.

The centerpiece is the spec's conflict test: local record says
SUBMITTED, exchange says FILLED — the local record must be corrected
to FILLED, a Fill backfilled, and ExchangeOrderMismatchDetected +
ExchangeOrderCorrected BOTH published. Never a silent correction.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.events import (
    ExchangeOrderCorrected,
    ExchangeOrderMismatchDetected,
)
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import Fill, Order, OrderState, OrderType
from core.execution.order_manager import OrderManager
from core.execution.reconciliation_job import ReconciliationJob
from core.risk.risk_decision import SizingDecision

ACCOUNT_ID = "test_recon_account"
STRATEGY_ID = "test_strategy_recon"


class FakeExchangeSideAdapter(ExecutionAdapter):
    """submit_order() leaves the order SUBMITTED (like the real
    adapter); get_order_status() returns whatever exchange state the
    test scripted, as a snapshot copy — mirroring
    BinanceExecutionAdapter._report_order_snapshot()."""

    def __init__(self):
        self._orders: dict[str, Order] = {}
        self.exchange_states: dict[str, OrderState] = {}
        self.exchange_fills: dict[str, list[Fill]] = {}
        self.status_calls: list[str] = []

    def submit_order(self, order: Order) -> Order:
        order.transition_to(OrderState.SUBMITTED, datetime.now(UTC))
        self._orders[order.client_order_id] = order
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        raise NotImplementedError

    def get_order_status(self, client_order_id: str) -> Order:
        self.status_calls.append(client_order_id)
        order = self._orders[client_order_id]
        state = self.exchange_states.get(client_order_id, order.state)
        return replace(order, state=state)

    def get_fills(self, client_order_id: str) -> list[Fill]:
        return list(self.exchange_fills.get(client_order_id, []))


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError

    def of_type(self, event_cls):
        return [e for e in self.published if isinstance(e, event_cls)]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("""
            DELETE FROM reconciliation_log WHERE client_order_id IN
                (SELECT client_order_id FROM orders WHERE strategy_id = :s)
            """),
            {"s": STRATEGY_ID},
        )
        session.execute(
            text("""
            DELETE FROM fills WHERE client_order_id IN
                (SELECT client_order_id FROM orders WHERE strategy_id = :s)
            """),
            {"s": STRATEGY_ID},
        )
        session.execute(text("DELETE FROM orders WHERE strategy_id = :s"), {"s": STRATEGY_ID})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.commit()
        session.close()


@pytest.fixture
def setup(db):
    """A live-mode OrderManager wired to the fake adapter, one order
    submitted and sitting locally at SUBMITTED."""
    adapter = FakeExchangeSideAdapter()
    event_bus = FakeEventBus()
    manager = OrderManager(
        execution_adapter=adapter,
        event_bus=event_bus,
        db_session=db,
        mode="live",
        account_id=ACCOUNT_ID,
        starting_balance=100_000.0,
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.commit()

    order = manager.submit(
        sizing_decision=SizingDecision(
            approved_quantity=0.01, proposed_quantity=0.01, risk_decision_id=decision_id
        ),
        strategy_id=STRATEGY_ID,
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
    )
    assert order.state == OrderState.SUBMITTED
    return adapter, event_bus, manager, order


def make_job(db, adapter, manager, event_bus, interval_seconds=60.0):
    return ReconciliationJob(
        db=db,
        adapter=adapter,
        order_manager=manager,
        event_bus=event_bus,
        interval_seconds=interval_seconds,
    )


def test_clean_check_logs_but_publishes_nothing(db, setup):
    adapter, event_bus, manager, order = setup
    event_bus.published.clear()
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert len(results) == 1
    assert results[0].mismatch is False
    assert results[0].corrected is False
    assert event_bus.of_type(ExchangeOrderMismatchDetected) == []
    row = (
        db.execute(
            text("SELECT mismatch, corrected FROM reconciliation_log WHERE client_order_id = :o"),
            {"o": order.client_order_id},
        )
        .mappings()
        .one()
    )
    assert row["mismatch"] is False  # a clean check still leaves evidence


def test_conflict_local_submitted_exchange_filled_is_corrected_with_backfilled_fill(db, setup):
    """The spec's reconciliation conflict test, verbatim."""
    adapter, event_bus, manager, order = setup
    event_bus.published.clear()
    adapter.exchange_states[order.client_order_id] = OrderState.FILLED
    adapter.exchange_fills[order.client_order_id] = [
        Fill(
            client_order_id=order.client_order_id,
            fill_price=65000.0,
            quantity=0.01,
            fee=0.65,
            filled_at=datetime.now(UTC),
            is_partial=False,
        )
    ]
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert results[0].mismatch is True
    assert results[0].corrected is True

    # Local record corrected to FILLED — in memory and in Postgres.
    assert order.state == OrderState.FILLED
    db_state = db.execute(
        text("SELECT state FROM orders WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert db_state == "filled"

    # The Fill was backfilled through the one shared fill path.
    fill_count = db.execute(
        text("SELECT count(*) FROM fills WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert fill_count == 1

    # BOTH events published — never a silent correction.
    assert len(event_bus.of_type(ExchangeOrderMismatchDetected)) == 1
    assert len(event_bus.of_type(ExchangeOrderCorrected)) == 1


def test_conflict_exchange_cancelled_corrects_the_local_record(db, setup):
    adapter, event_bus, manager, order = setup
    event_bus.published.clear()
    adapter.exchange_states[order.client_order_id] = OrderState.CANCELLED
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert results[0].corrected is True
    db_state = db.execute(
        text("SELECT state FROM orders WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert db_state == "cancelled"
    assert len(event_bus.of_type(ExchangeOrderCorrected)) == 1


def test_illegal_correction_is_surfaced_not_forced(db, setup):
    """local PENDING_CANCEL + exchange REJECTED can't be expressed as a
    legal transition — a genuine anomaly. It must be reported
    (mismatch event, corrected=False in the log), never forced through
    the state machine."""
    adapter, event_bus, manager, order = setup
    event_bus.published.clear()
    # Force the local record to PENDING_CANCEL (a legal move from SUBMITTED).
    order.transition_to(OrderState.PENDING_CANCEL, datetime.now(UTC))
    db.execute(
        text("UPDATE orders SET state = 'pending_cancel' WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    )
    db.commit()
    adapter.exchange_states[order.client_order_id] = OrderState.REJECTED
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert results[0].mismatch is True
    assert results[0].corrected is False
    assert len(event_bus.of_type(ExchangeOrderMismatchDetected)) == 1
    assert event_bus.of_type(ExchangeOrderCorrected) == []
    db_state = db.execute(
        text("SELECT state FROM orders WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert db_state == "pending_cancel"  # untouched, left for a human


def test_filled_on_exchange_but_no_fills_reported_is_not_corrected(db, setup):
    adapter, event_bus, manager, order = setup
    event_bus.published.clear()
    adapter.exchange_states[order.client_order_id] = OrderState.FILLED
    # deliberately NO fills scripted
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert results[0].mismatch is True
    assert results[0].corrected is False
    assert order.state == OrderState.SUBMITTED  # never transitioned without a fill to back it


def test_paper_orders_are_never_reconciled(db, setup):
    adapter, event_bus, manager, order = setup
    db.execute(
        text("UPDATE orders SET mode = 'paper' WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    )
    db.commit()
    job = make_job(db, adapter, manager, event_bus)

    results = job.run_once()

    assert results == []
    assert adapter.status_calls == []


def test_is_due_enforces_the_configured_cadence(db, setup):
    adapter, event_bus, manager, order = setup
    job = make_job(db, adapter, manager, event_bus, interval_seconds=60.0)
    t0 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)

    assert job.is_due(t0) is True
    job.run_once(t0)
    assert job.is_due(t0 + timedelta(seconds=30)) is False
    assert job.is_due(t0 + timedelta(seconds=60)) is True


def test_scheduler_triggers_reconciliation_when_due(db, setup):
    """The spec's integration point: scheduled via the existing
    Scheduler, not a new mechanism."""
    from core.ingestion.config import IngestionConfig
    from core.ingestion.scheduler import Scheduler

    adapter, event_bus, manager, order = setup
    job = make_job(db, adapter, manager, event_bus)
    scheduler = Scheduler(db, adapters={}, config=IngestionConfig(), reconciliation_job=job)

    summary = scheduler.run_once(now=datetime(2024, 6, 1, 12, 0, tzinfo=UTC))
    assert summary.reconciliations_run == 1

    # Immediately after, the job's own cadence says not due — the
    # sweep runs, but reconciliation doesn't re-fire.
    summary2 = scheduler.run_once(now=datetime(2024, 6, 1, 12, 0, 30, tzinfo=UTC))
    assert summary2.reconciliations_run == 0
