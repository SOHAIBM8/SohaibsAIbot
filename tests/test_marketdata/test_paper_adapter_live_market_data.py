"""
End-to-end proof of step 6: a real local WebSocket server streams
normalized-tick JSON, LiveMarketDataSource consumes and caches it, and
PaperExecutionAdapter fills a MARKET order at that live-streamed price
— with zero code changes to PaperExecutionAdapter itself, since it was
already written against the MarketDataSource Protocol.
"""

import asyncio
import time
from datetime import UTC, datetime

from core.execution.latency_simulator import LatencySimulator
from core.execution.order import Order, OrderState, OrderType
from core.execution.paper_execution_adapter import PaperExecutionAdapter
from core.execution_model import ExecutionModel
from core.marketdata.live_market_data_source import LiveMarketDataSource


def wait_until(predicate, timeout_s=10.0, interval_s=0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def make_order(symbol="BTC/USDT") -> Order:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    return Order(
        client_order_id="co-live-1",
        strategy_id="s1",
        symbol=symbol,
        order_type=OrderType.MARKET,
        direction=1,
        quantity=1.0,
        limit_price=None,
        stop_price=None,
        mode="paper",
        state=OrderState.PENDING,
        risk_decision_id=1,
        created_at=now,
        updated_at=now,
    )


def test_paper_order_fills_at_live_streamed_price(fake_server):
    async def handler(ws):
        await ws.send(
            '{"symbol": "BTC/USDT", "price": 71234.5, "timestamp": "2024-06-01T12:00:00+00:00"}'
        )
        await asyncio.Future()  # keep the connection open

    server = fake_server(handler)
    market_data = LiveMarketDataSource(url=f"ws://localhost:{server.port}", heartbeat_timeout_s=5.0)
    market_data.start()
    try:
        assert wait_until(lambda: market_data.is_connected())
        assert wait_until(lambda: _has_price(market_data, "BTC/USDT"), timeout_s=5.0)

        adapter = PaperExecutionAdapter(
            execution_model=ExecutionModel(fee_bps=0.0, slippage_bps=0.0),
            latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
            market_data_source=market_data,
        )

        order = adapter.submit_order(make_order())

        assert order.state == OrderState.SUBMITTED
        fills = adapter.get_fills("co-live-1")
        assert len(fills) == 1
        assert fills[0].fill_price == 71234.5
    finally:
        market_data.stop()


def _has_price(source: LiveMarketDataSource, symbol: str) -> bool:
    try:
        source.get_last_price(symbol)
        return True
    except KeyError:
        return False
