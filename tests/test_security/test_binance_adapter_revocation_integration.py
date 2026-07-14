"""
The concrete proof behind step 8's design decision (confirmed with the
user before implementing): a REAL BinanceExecutionAdapter instance,
already constructed and having already placed one order successfully,
must be stopped by EmergencyCredentialRevocation on its very NEXT call
— not just block a future adapter that hasn't been built yet. No real
network calls — a fake requests.Session, matching every other
Binance-facing test in this project.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.binance_clock_sync import ClockSyncService
from core.execution.binance_execution_adapter import BinanceExecutionAdapter
from core.execution.binance_symbol_filter_cache import SymbolFilterCache
from core.execution.order import Order, OrderState, OrderType
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider, CredentialRevokedError
from core.security.credential_vault import CredentialVault
from core.security.emergency_revocation import EmergencyCredentialRevocation
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_bear_account"


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text_body

    def json(self):
        return self._json_body


class _ScriptedSession:
    def __init__(self):
        self.queues: dict[str, list] = {"get": [], "post": [], "delete": []}
        self.calls: list[tuple[str, str, dict]] = []

    def script(self, method: str, result) -> None:
        self.queues[method].append(result)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("get", url, params)

    def post(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("post", url, params)

    def delete(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("delete", url, params)

    def _dispatch(self, method, url, params):
        self.calls.append((method, url, params or {}))
        return self.queues[method].pop(0)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("""
            DELETE FROM credential_revocation WHERE credential_id IN
                (SELECT credential_id FROM encrypted_credentials WHERE account_id = :a)
            """),
            {"a": ACCOUNT_ID},
        )
        session.execute(
            text("""
            DELETE FROM credential_audit_log WHERE credential_id IN
                (SELECT credential_id FROM encrypted_credentials WHERE account_id = :a)
            """),
            {"a": ACCOUNT_ID},
        )
        session.execute(
            text("DELETE FROM encrypted_credentials WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
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


def _filters_response():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001",
                        "maxQty": "9000",
                        "stepSize": "0.00001",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1000000",
                        "tickSize": "0.01",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
                ],
            }
        ]
    }


def make_order(client_order_id="co-revocation-1") -> Order:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    return Order(
        client_order_id=client_order_id,
        strategy_id="s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
        quantity=0.01,
        limit_price=None,
        stop_price=None,
        mode="live",
        state=OrderState.PENDING,
        risk_decision_id=1,
        created_at=now,
        updated_at=now,
    )


def test_a_live_adapter_is_stopped_by_revocation_on_its_very_next_call(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_BEAR_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.ACTIVE)
    revocation = EmergencyCredentialRevocation(db, manager)
    provider = CredentialProvider(manager, vault, audit_db, revocation=revocation)

    session = _ScriptedSession()
    clock_sync = ClockSyncService(
        base_url="https://testnet.binance.vision", session=session, clock=lambda: 1_700_000_000.0
    )
    filter_cache = SymbolFilterCache(base_url="https://testnet.binance.vision", session=session)
    session.script("get", _FakeResponse(200, json_body=_filters_response()))
    filter_cache.refresh()
    session.calls = []

    adapter = BinanceExecutionAdapter(
        base_url="https://testnet.binance.vision",
        clock_sync=clock_sync,
        filter_cache=filter_cache,
        credential_provider=provider,
        credential_id=credential_id,
        session=session,
    )

    # 1. The adapter works normally BEFORE any revocation.
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 1, "status": "NEW", "fills": []})
    )
    order = adapter.submit_order(make_order("co-revocation-1"))
    assert order.state == OrderState.SUBMITTED

    # 2. Revoke — the credential is now blocked, mid-session, on an
    # adapter instance that was already fully constructed and already
    # used successfully.
    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")

    # 3. The VERY NEXT call — no reconstruction, same adapter object —
    # is rejected BEFORE it ever reaches the exchange (no POST is sent;
    # the queue is left untouched, proving the network call was never
    # attempted).
    with pytest.raises(CredentialRevokedError):
        adapter.submit_order(make_order("co-revocation-2"))

    post_calls_after_revocation = [
        c
        for c in session.calls
        if c[0] == "post" and c[2].get("newClientOrderId") == "co-revocation-2"
    ]
    assert post_calls_after_revocation == []
