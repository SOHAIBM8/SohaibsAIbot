"""
ExternalTradeDetectionService against real local Postgres (orders,
external_trade_log rows are real) with a fake ExchangeOrderLister
returning scripted exchange responses — no real network, same pattern
every other exchange-facing component's tests use.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.events import ExternalTradeDetected
from core.execution.external_trade_detection_service import ExternalTradeDetectionService
from core.ingestion.event_bus import EventBus, EventHandler

ACCOUNT_ID = "test_external_trade_account"
STRATEGY_ID = "test_external_trade_strategy"
SYMBOL = "BTC/USDT"


class _FakeOrderLister:
    def __init__(self, orders_by_symbol: dict[str, list[dict]]):
        self._orders_by_symbol = orders_by_symbol
        self.calls: list[str] = []

    def list_open_orders(self, symbol: str) -> list[dict]:
        self.calls.append(symbol)
        return self._orders_by_symbol.get(symbol, [])


class _RecordingEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        raise NotImplementedError


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM external_trade_log WHERE symbol = :s"), {"s": SYMBOL})
        session.execute(text("DELETE FROM orders WHERE strategy_id = :s"), {"s": STRATEGY_ID})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.commit()
        session.close()


def _insert_known_order(db, client_order_id: str, mode: str = "live") -> None:
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at)
            VALUES
                (:client_order_id, :strategy_id, :symbol, 'MARKET', 1, 1.0,
                 :mode, 'SUBMITTED', :risk_decision_id, :now, :now)
            """),
        {
            "client_order_id": client_order_id,
            "strategy_id": STRATEGY_ID,
            "symbol": SYMBOL,
            "mode": mode,
            "risk_decision_id": decision_id,
            "now": datetime.now(UTC),
        },
    )
    db.commit()


def test_no_symbols_with_live_orders_checks_nothing(db):
    lister = _FakeOrderLister({SYMBOL: [{"clientOrderId": "unknown", "orderId": 1}]})
    service = ExternalTradeDetectionService(db, lister)

    results = service.run_once()

    assert results == []
    assert lister.calls == []  # never even asked — no live symbols tracked yet


def test_matching_client_order_id_is_not_flagged(db):
    _insert_known_order(db, "co-known-1")
    lister = _FakeOrderLister({SYMBOL: [{"clientOrderId": "co-known-1", "orderId": 1}]})
    service = ExternalTradeDetectionService(db, lister)

    results = service.run_once()

    assert results == []


def test_unmatched_client_order_id_is_flagged_and_recorded(db):
    _insert_known_order(db, "co-known-1")
    lister = _FakeOrderLister(
        {
            SYMBOL: [
                {"clientOrderId": "co-known-1", "orderId": 1},
                {
                    "clientOrderId": "manual-ui-order",
                    "orderId": 2,
                    "side": "SELL",
                    "status": "NEW",
                },
            ]
        }
    )
    service = ExternalTradeDetectionService(db, lister)

    results = service.run_once()

    assert len(results) == 1
    assert results[0].exchange_order_id == "2"
    assert results[0].exchange_client_order_id == "manual-ui-order"
    assert results[0].newly_recorded is True

    row = (
        db.execute(text("SELECT * FROM external_trade_log WHERE exchange_order_id = '2'"))
        .mappings()
        .first()
    )
    assert row is not None
    assert row["side"] == "SELL"
    assert row["status"] == "NEW"


def test_paper_mode_orders_are_never_treated_as_known(db):
    """A paper client_order_id could never actually appear on a real
    exchange, but if it somehow did, it must still be flagged — only
    LIVE orders count as 'ours' for this check. A live order for the
    same symbol exists too, so the symbol is actually watched — the
    paper order alone would never even get this symbol scanned (see
    test_no_symbols_with_live_orders_checks_nothing)."""
    _insert_known_order(db, "co-live-1", mode="live")
    _insert_known_order(db, "co-paper-1", mode="paper")
    lister = _FakeOrderLister(
        {
            SYMBOL: [
                {"clientOrderId": "co-live-1", "orderId": 1},
                {"clientOrderId": "co-paper-1", "orderId": 5},
            ]
        }
    )
    service = ExternalTradeDetectionService(db, lister)

    results = service.run_once()

    assert len(results) == 1
    assert results[0].exchange_order_id == "5"


def test_event_bus_receives_external_trade_detected_for_a_new_row(db):
    _insert_known_order(db, "co-known-1")
    lister = _FakeOrderLister(
        {SYMBOL: [{"clientOrderId": "manual-1", "orderId": 9, "side": "BUY", "status": "NEW"}]}
    )
    bus = _RecordingEventBus()
    service = ExternalTradeDetectionService(db, lister, exchange="binance", event_bus=bus)

    service.run_once()

    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, ExternalTradeDetected)
    assert event.exchange == "binance"
    assert event.symbol == SYMBOL
    assert event.exchange_order_id == "9"
    assert event.exchange_client_order_id == "manual-1"


def test_does_not_republish_for_an_already_recorded_external_order(db):
    _insert_known_order(db, "co-known-1")
    lister = _FakeOrderLister(
        {SYMBOL: [{"clientOrderId": "manual-1", "orderId": 9, "side": "BUY", "status": "NEW"}]}
    )
    bus = _RecordingEventBus()
    service = ExternalTradeDetectionService(db, lister, event_bus=bus)

    service.run_once()
    assert len(bus.published) == 1

    # Same still-open external order, re-scanned on a later cycle.
    results = service.run_once()

    assert results[0].newly_recorded is False
    assert len(bus.published) == 1  # no second event


def test_no_event_published_without_an_event_bus(db):
    _insert_known_order(db, "co-known-1")
    lister = _FakeOrderLister(
        {SYMBOL: [{"clientOrderId": "manual-1", "orderId": 9, "side": "BUY", "status": "NEW"}]}
    )
    service = ExternalTradeDetectionService(db, lister)

    # Constructed without event_bus — must not raise.
    results = service.run_once()

    assert len(results) == 1


def test_is_due_true_on_first_call_then_respects_interval():
    lister = _FakeOrderLister({})
    service = ExternalTradeDetectionService(SessionLocal(), lister, interval_seconds=60.0)
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

    assert service.is_due(now) is True
    service.run_once(now)
    assert service.is_due(now) is False
    assert service.is_due(now.replace(minute=1, second=1)) is True
