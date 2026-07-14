from datetime import UTC, datetime

import pytest

from core.execution.latency_simulator import LatencySimulator
from core.execution.order import Order, OrderState, OrderType
from core.execution.paper_execution_adapter import PaperExecutionAdapter
from core.execution_model import ExecutionModel


class _FakeMarketDataSource:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def get_last_price(self, symbol: str) -> float:
        return self._prices[symbol]


def make_order(
    client_order_id="co-1",
    order_type=OrderType.MARKET,
    direction=1,
    quantity=1.0,
    limit_price=None,
    stop_price=None,
    symbol="BTC/USDT",
) -> Order:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    return Order(
        client_order_id=client_order_id,
        strategy_id="s1",
        symbol=symbol,
        order_type=order_type,
        direction=direction,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        mode="paper",
        state=OrderState.PENDING,
        risk_decision_id=1,
        created_at=now,
        updated_at=now,
    )


def make_adapter(prices=None, fee_bps=0.0, slippage_bps=0.0):
    return PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=fee_bps, slippage_bps=slippage_bps),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource(prices or {"BTC/USDT": 100.0}),
    )


def test_market_order_fills_at_last_market_price():
    adapter = make_adapter(prices={"BTC/USDT": 100.0})
    order = make_order(order_type=OrderType.MARKET)

    result = adapter.submit_order(order)

    # submit_order() only takes the order to SUBMITTED — OrderManager
    # (step 4) owns the FILLED transition via handle_fill(), even
    # though the fill itself is already computed and available here.
    assert result.state == OrderState.SUBMITTED
    fills = adapter.get_fills("co-1")
    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(100.0)


def test_limit_order_fills_at_its_limit_price():
    adapter = make_adapter(prices={"BTC/USDT": 999.0})  # should be ignored
    order = make_order(order_type=OrderType.LIMIT, limit_price=90.0)

    adapter.submit_order(order)

    assert adapter.get_fills("co-1")[0].fill_price == pytest.approx(90.0)


def test_stop_order_fills_at_its_stop_price():
    adapter = make_adapter(prices={"BTC/USDT": 999.0})
    order = make_order(order_type=OrderType.STOP, stop_price=80.0)

    adapter.submit_order(order)

    assert adapter.get_fills("co-1")[0].fill_price == pytest.approx(80.0)


def test_submit_transitions_pending_to_submitted():
    adapter = make_adapter()
    order = make_order()
    assert order.state == OrderState.PENDING

    result = adapter.submit_order(order)

    assert result.state == OrderState.SUBMITTED
    assert adapter.get_order_status("co-1").state == OrderState.SUBMITTED


def test_resubmitting_the_same_client_order_id_is_idempotent():
    """spec decision #2: client_order_id is the sole idempotency key."""
    adapter = make_adapter()
    order = make_order()

    first = adapter.submit_order(order)
    second = adapter.submit_order(make_order())  # a fresh Order object, same id

    assert first is second  # returns the original, not a new fill
    assert len(adapter.get_fills("co-1")) == 1  # never double-filled


def test_get_order_status_raises_for_unknown_order():
    adapter = make_adapter()
    with pytest.raises(KeyError):
        adapter.get_order_status("does-not-exist")


def test_get_fills_returns_empty_list_for_unknown_order():
    adapter = make_adapter()
    assert adapter.get_fills("does-not-exist") == []


def test_cancel_transitions_submitted_order_to_cancelled():
    adapter = make_adapter()
    adapter.submit_order(make_order())  # ends at SUBMITTED, not FILLED

    result = adapter.cancel_order("co-1")

    assert result.state == OrderState.CANCELLED


def test_cancel_a_filled_order_raises():
    adapter = make_adapter()
    order = adapter.submit_order(make_order())
    # Simulate what OrderManager's handle_fill() would do with the fill
    # this adapter already computed — the adapter itself never performs
    # this transition (see module docstring), so the test does it
    # directly to exercise the invariant at the adapter/state-machine
    # boundary: cancelling an already-filled order must still fail.
    order.transition_to(OrderState.FILLED, datetime.now(UTC))

    with pytest.raises(ValueError, match="illegal order state transition"):
        adapter.cancel_order("co-1")


def test_sell_order_direction_is_passed_through_to_fill_simulator():
    adapter = make_adapter(prices={"BTC/USDT": 100.0}, slippage_bps=10.0)
    order = make_order(direction=-1)

    adapter.submit_order(order)

    # Sell: slippage adverse means fill price is BELOW reference.
    assert adapter.get_fills("co-1")[0].fill_price < 100.0


def test_load_order_allows_cancel_on_an_adapter_that_never_submitted_it():
    """The dashboard's cancel-order endpoint constructs a fresh adapter
    per request — this proves that adapter can still cancel an order a
    DIFFERENT (earlier) adapter instance originally submitted, once
    rehydrated via load_order()."""
    adapter = make_adapter()
    order = make_order()
    order.state = OrderState.SUBMITTED  # as it would be, read back from Postgres

    adapter.load_order(order)
    result = adapter.cancel_order("co-1")

    assert result.state == OrderState.CANCELLED


def test_load_order_then_cancel_on_a_filled_order_still_raises():
    adapter = make_adapter()
    order = make_order()
    order.state = OrderState.FILLED

    adapter.load_order(order)

    with pytest.raises(ValueError, match="illegal order state transition"):
        adapter.cancel_order("co-1")


def test_load_order_also_makes_get_order_status_work():
    adapter = make_adapter()
    order = make_order()
    order.state = OrderState.SUBMITTED

    adapter.load_order(order)

    assert adapter.get_order_status("co-1").state == OrderState.SUBMITTED


class _RecordingEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


def test_publishes_paper_fill_simulated_with_latency_when_event_bus_given():
    event_bus = _RecordingEventBus()
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=0.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=42.0, jitter_ms=0.0),
        market_data_source=_FakeMarketDataSource({"BTC/USDT": 100.0}),
        event_bus=event_bus,
    )

    adapter.submit_order(make_order())

    assert len(event_bus.published) == 1
    event = event_bus.published[0]
    assert event.event_type == "PaperFillSimulated"
    assert event.client_order_id == "co-1"
    assert event.simulated_latency_ms == 42.0


def test_no_event_published_when_no_event_bus_given():
    adapter = make_adapter()  # constructed without event_bus
    adapter.submit_order(make_order())  # must not raise
