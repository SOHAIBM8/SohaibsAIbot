"""
Real Binance TESTNET integration suite (spec section 8, final bullet).
Everything here makes real network calls to testnet.binance.vision with
real (testnet-only, valueless) credentials — it is marked `testnet` and
skipped entirely unless BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET
are set in the environment. Also needs real local Postgres running
(docker compose up -d), same as the standard suite.

Confirmed scoping (spec open decision #3): these are a CORRECTNESS
check — testnet liquidity is thin and occasionally resets, so fill
prices/latency here are not a performance or slippage benchmark.

What it proves end-to-end, per the spec:
1. a real testnet MARKET order placed through the full OrderManager ->
   BinanceExecutionAdapter path, filled, with the fill applied to the
   local record;
2. a real testnet LIMIT order (priced far from market so it rests)
   cancelled cleanly;
3. a real WebSocket fill notification arriving via
   BinanceOrderStreamConsumer that matches what ReconciliationJob
   independently confirms via REST.

Note on (3): the original run of this suite against real testnet
uncovered that Binance's listenKey-based user data stream
(`POST /api/v3/userDataStream`) returns 410 Gone — deprecated in favor
of a signed `userDataStream.subscribe.signature` request sent directly
over the WebSocket API connection (see
core/execution/binance_order_stream_consumer.py's module docstring).
This test targets that WebSocket API endpoint directly; there is no
ListenKeyManager anymore.
"""

import os
import time
from datetime import UTC, datetime

import pytest
import requests
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.binance_clock_sync import ClockSyncService
from core.execution.binance_execution_adapter import BinanceExecutionAdapter
from core.execution.binance_order_stream_consumer import BinanceOrderStreamConsumer
from core.execution.binance_symbol_filter_cache import SymbolFilterCache
from core.execution.order import OrderState, OrderType
from core.execution.order_manager import OrderManager
from core.execution.reconciliation_job import ReconciliationJob
from core.risk.risk_decision import SizingDecision
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

TESTNET_REST = "https://testnet.binance.vision"
TESTNET_WS_API = "wss://ws-api.testnet.binance.vision/ws-api/v3"
SYMBOL = "BTC/USDT"
BINANCE_SYMBOL = "BTCUSDT"
ACCOUNT_ID = "testnet_integration_account"
STRATEGY_ID = "testnet_integration_strategy"

_HAVE_CREDS = bool(
    os.environ.get("BINANCE_TESTNET_API_KEY") and os.environ.get("BINANCE_TESTNET_API_SECRET")
)

pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        not _HAVE_CREDS,
        reason="BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET not set — "
        "testnet integration suite requires real (testnet-only) credentials",
    ),
]


def wait_until(predicate, timeout_s=30.0, interval_s=0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


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
        # This suite submits mode="live" orders — real fills route to
        # live_accounts now, not paper_accounts (docs/gap_audit_report.md
        # P0 #1). Clean up both; whichever one this run actually wrote to
        # is the only one with a row to delete either way.
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.execute(text("DELETE FROM live_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.commit()
        session.close()


@pytest.fixture
def audit_db():
    session = AuditWriterSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def vault() -> CredentialVault:
    # ONE vault/KMS client instance shared by every fixture in this
    # test — a second LocalDevKMSClient instance without the env var
    # set would generate its OWN ephemeral KEK and fail to decrypt
    # anything encrypted under the first one.
    return CredentialVault(LocalDevKMSClient(kek_env_var="TESTNET_INTEGRATION_KEK"))


@pytest.fixture
def credential_id(db, vault):
    """Registers the real testnet credentials (from the same env vars
    the suite is gated on) through the real Stage 3 vault/lifecycle
    path — this is the actual CredentialProvider seam Stage 2's adapter
    now depends on (decision #7), not a shortcut around it."""
    manager = KeyLifecycleManager(db, vault)
    cred_id = manager.register(
        account_id=ACCOUNT_ID,
        exchange="binance",
        api_key=os.environ["BINANCE_TESTNET_API_KEY"],
        api_secret=os.environ["BINANCE_TESTNET_API_SECRET"],
        mainnet=False,
    )
    manager.transition(cred_id, CredentialState.ACTIVE)
    yield cred_id
    db.execute(text("DELETE FROM credential_audit_log WHERE credential_id = :c"), {"c": cred_id})
    db.execute(text("DELETE FROM encrypted_credentials WHERE credential_id = :c"), {"c": cred_id})
    db.commit()


@pytest.fixture
def stack(db, audit_db, vault, credential_id):
    """The full real Stage 2+3 stack against testnet: clock sync,
    filter cache, credential provider, adapter, order manager."""
    clock_sync = ClockSyncService(base_url=TESTNET_REST)
    clock_sync.sync()

    filter_cache = SymbolFilterCache(base_url=TESTNET_REST)
    filter_cache.refresh(symbols=[BINANCE_SYMBOL])

    manager = KeyLifecycleManager(db, vault)
    credential_provider = CredentialProvider(manager, vault, audit_db)

    adapter = BinanceExecutionAdapter(
        base_url=TESTNET_REST,
        clock_sync=clock_sync,
        filter_cache=filter_cache,
        credential_provider=credential_provider,
        credential_id=credential_id,
        db_session=db,
    )

    class _NullEventBus:
        def publish(self, event):
            pass

        def subscribe(self, event_type, handler):
            pass

    manager = OrderManager(
        execution_adapter=adapter,
        event_bus=_NullEventBus(),
        db_session=db,
        mode="live",
        account_id=ACCOUNT_ID,
        starting_balance=100_000.0,
    )
    return adapter, manager, filter_cache


def _make_sizing_decision(db, quantity: float) -> SizingDecision:
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, :q, '[]') RETURNING id
            """),
        {"t": datetime.now(UTC), "s": STRATEGY_ID, "q": quantity},
    ).scalar_one()
    db.commit()
    return SizingDecision(
        approved_quantity=quantity, proposed_quantity=quantity, risk_decision_id=decision_id
    )


def _current_price() -> float:
    response = requests.get(
        f"{TESTNET_REST}/api/v3/ticker/price", params={"symbol": BINANCE_SYMBOL}, timeout=10
    )
    response.raise_for_status()
    return float(response.json()["price"])


def _market_quantity(filter_cache: SymbolFilterCache, price: float) -> float:
    """Smallest step-aligned quantity comfortably above min_notional."""
    filters = filter_cache.get(BINANCE_SYMBOL)
    target_notional = max(filters.min_notional * 1.5, 15.0)
    raw_qty = target_notional / price
    steps = int(raw_qty / filters.step_size) + 1
    return round(steps * filters.step_size, 8)


def test_market_order_fills_end_to_end(db, stack):
    adapter, manager, filter_cache = stack
    price = _current_price()
    quantity = _market_quantity(filter_cache, price)

    order = manager.submit(
        sizing_decision=_make_sizing_decision(db, quantity),
        strategy_id=STRATEGY_ID,
        symbol=SYMBOL,
        order_type=OrderType.MARKET,
        direction=1,
    )

    # OrderManager.submit() drains adapter.get_fills() synchronously —
    # a testnet market order on BTCUSDT fills immediately, so by the
    # time submit() returns, handle_fill() should already have run.
    assert order.exchange_order_id
    assert order.state == OrderState.FILLED

    fill_count = db.execute(
        text("SELECT count(*) FROM fills WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert fill_count >= 1


def test_limit_order_cancels_cleanly(db, stack):
    adapter, manager, filter_cache = stack
    price = _current_price()
    filters = filter_cache.get(BINANCE_SYMBOL)
    # 10% below market: far enough to rest instead of filling, close
    # enough to stay inside Binance's PERCENT_PRICE_BY_SIDE band (a
    # price-deviation-from-market filter SymbolFilterCache doesn't
    # model — out of the spec's decision #5 scope, which covers only
    # LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL). A 50% offset, tried first
    # against real testnet, tripped that filter and got rejected
    # (code -1013) — confirming the adapter's rejection handling works
    # correctly, but not what this test is meant to exercise.
    limit_price = round(int((price * 0.9) / filters.tick_size) * filters.tick_size, 8)
    quantity = _market_quantity(filter_cache, limit_price)

    order = manager.submit(
        sizing_decision=_make_sizing_decision(db, quantity),
        strategy_id=STRATEGY_ID,
        symbol=SYMBOL,
        order_type=OrderType.LIMIT,
        direction=1,
        limit_price=limit_price,
    )
    assert order.state == OrderState.SUBMITTED

    cancelled = manager.cancel(order.client_order_id)

    assert cancelled.state == OrderState.CANCELLED
    db_state = db.execute(
        text("SELECT state FROM orders WHERE client_order_id = :o"),
        {"o": order.client_order_id},
    ).scalar_one()
    assert db_state == "cancelled"


def test_websocket_fill_notification_matches_rest_reconciliation(db, stack):
    """The spec's step 6 flow verbatim: submit, get a WebSocket fill
    notification, confirm reconciliation independently agrees via REST."""
    adapter, manager, filter_cache = stack

    ws_fills_seen: list[str] = []

    class _RecordingOrderManager:
        """Wraps the real OrderManager so the test can observe which
        fills arrived via the WebSocket path specifically."""

        def handle_fill(self, fill):
            ws_fills_seen.append(fill.client_order_id)
            try:
                manager.handle_fill(fill)
            except (KeyError, ValueError):
                # submit()'s synchronous get_fills() drain may have
                # already applied this same fill — the stream is a
                # low-latency duplicate here, and ReconciliationJob
                # (REST) is authoritative either way (decision #4).
                pass

    consumer = BinanceOrderStreamConsumer(
        ws_url=TESTNET_WS_API,
        order_manager=_RecordingOrderManager(),
    )
    consumer.start()
    try:
        assert wait_until(consumer.is_connected, timeout_s=15.0), "user data stream never connected"

        price = _current_price()
        quantity = _market_quantity(filter_cache, price)
        order = manager.submit(
            sizing_decision=_make_sizing_decision(db, quantity),
            strategy_id=STRATEGY_ID,
            symbol=SYMBOL,
            order_type=OrderType.MARKET,
            direction=1,
        )

        # 1. The WebSocket notification arrives for our order.
        assert wait_until(
            lambda: order.client_order_id in ws_fills_seen, timeout_s=30.0
        ), "no executionReport TRADE event arrived on the user data stream"

        # 2. Reconciliation independently confirms via REST that the
        # exchange's view agrees with the local record.
        job = ReconciliationJob(db=db, adapter=adapter, order_manager=manager)
        job.run_once()
        row = (
            db.execute(
                text("""
                SELECT local_state, exchange_state, mismatch FROM reconciliation_log
                WHERE client_order_id = :o ORDER BY id DESC LIMIT 1
                """),
                {"o": order.client_order_id},
            )
            .mappings()
            .first()
        )
        if row is not None:  # order still open at check time -> logged
            assert row["mismatch"] is False
        # Whether or not the order was still open when reconciliation
        # swept, the REST-confirmed final state must be FILLED.
        snapshot = adapter.get_order_status(order.client_order_id)
        assert snapshot.state == OrderState.FILLED
    finally:
        consumer.stop()
