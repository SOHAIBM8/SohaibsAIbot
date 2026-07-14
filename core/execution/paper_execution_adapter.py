"""
Stage 1's concrete ExecutionAdapter. Stage 1 has no real order book or
exchange, so every order fills synchronously and fully the moment it's
submitted — there is no partial-fill or resting-order behavior to
simulate yet (that's meaningful only once Stage 2 introduces a real
matching venue). Idempotent on client_order_id (spec decision #2): a
second submit_order() call with an already-known id returns the
existing order unchanged rather than re-transitioning or re-filling it.

Design note (rule 9, revised while building OrderManager in step 4):
submit_order() transitions the order to SUBMITTED only, NOT FILLED.
The spec's OrderManager.handle_fill() docstring says handle_fill() is
what "transitions order state (-> PARTIALLY_FILLED or FILLED)" — if
this adapter also drove the order to FILLED, handle_fill() would then
attempt an illegal FILLED -> FILLED transition the moment OrderManager
processed the fill it fetches via get_fills(). The fill itself is
still computed and stored synchronously here (Stage 1 has no async
exchange to wait on); only the ORDER STATE transition is deferred to
OrderManager, which is the single owner of every fill-driven
transition for both paper and live, per decision #1.

Also takes an optional `event_bus` (not in the spec's literal 3-param
constructor list) so it can publish PaperFillSimulated itself —
`simulated_latency_ms` is data FillSimulator/SimulatedFill compute
internally and never crosses the ExecutionAdapter interface (Fill has
no latency field), so nothing else could publish this event
meaningfully. Optional and defaulted to None so step 3's existing tests
(constructed without an event bus) keep working unchanged.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol

from core.execution.events import PaperFillSimulated
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.fill_simulator import FillSimulator
from core.execution.latency_simulator import LatencySimulator
from core.execution.order import Fill, Order, OrderState
from core.execution_model import ExecutionModel
from core.ingestion.event_bus import EventBus


class MarketDataSource(Protocol):
    """Minimal contract PaperExecutionAdapter needs from market data —
    satisfied by a simple fake in Stage 1 tests; step 6 wires the real
    normalized WebSocket feed (core/marketdata/) in behind this same
    Protocol without PaperExecutionAdapter changing at all."""

    def get_last_price(self, symbol: str) -> float: ...


class PaperExecutionAdapter(ExecutionAdapter):
    def __init__(
        self,
        execution_model: ExecutionModel,
        latency_simulator: LatencySimulator,
        market_data_source: MarketDataSource,
        event_bus: EventBus | None = None,
    ):
        self.fill_simulator = FillSimulator(execution_model, latency_simulator)
        self.market_data_source = market_data_source
        self.event_bus = event_bus
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = defaultdict(list)

    def load_order(self, order: Order) -> None:
        """Seeds this adapter's in-memory cache from an already-
        persisted order this SPECIFIC instance never itself submitted.

        Added for the dashboard's cancel-order control action
        (docs/dashboard_ui_spec.md section 11): Stage 1's adapter was
        built assuming one long-lived process handles submit->cancel
        for an order's whole lifecycle, sharing one in-memory
        `self._orders` cache throughout. A stateless HTTP API process
        constructs a fresh adapter per request and never called
        submit_order() itself, so cancel_order()'s `_require_order()`
        would always KeyError without this — the adapter has no way to
        know an order exists otherwise. This does not change any
        transition/fill logic; it only rehydrates state cancel_order()
        already expects to find in the cache, from the same Postgres
        row core/execution/order_reader.py already reads."""
        self._orders[order.client_order_id] = order

    def submit_order(self, order: Order) -> Order:
        if order.client_order_id in self._orders:
            return self._orders[order.client_order_id]

        order.transition_to(OrderState.SUBMITTED, datetime.now(UTC))
        self._orders[order.client_order_id] = order

        reference_price = self._reference_price(order)
        simulated = self.fill_simulator.simulate(reference_price, order.direction, order.quantity)

        filled_at = datetime.now(UTC)
        fill = Fill(
            client_order_id=order.client_order_id,
            fill_price=simulated.fill_price,
            quantity=simulated.quantity,
            fee=simulated.fee,
            filled_at=filled_at,
            is_partial=False,
        )
        self._fills[order.client_order_id].append(fill)

        if self.event_bus is not None:
            self.event_bus.publish(
                PaperFillSimulated(
                    client_order_id=order.client_order_id,
                    simulated_latency_ms=simulated.latency_ms,
                    occurred_at=filled_at,
                )
            )
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        order = self._require_order(client_order_id)
        order.transition_to(OrderState.CANCELLED, datetime.now(UTC))
        return order

    def get_order_status(self, client_order_id: str) -> Order:
        return self._require_order(client_order_id)

    def get_fills(self, client_order_id: str) -> list[Fill]:
        return list(self._fills.get(client_order_id, []))

    def _reference_price(self, order: Order) -> float:
        # No real matching engine in Stage 1: LIMIT/STOP orders fill
        # immediately at their stated price; MARKET orders fill at the
        # last known market price.
        if order.limit_price is not None:
            return order.limit_price
        if order.stop_price is not None:
            return order.stop_price
        return self.market_data_source.get_last_price(order.symbol)

    def _require_order(self, client_order_id: str) -> Order:
        if client_order_id not in self._orders:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        return self._orders[client_order_id]
