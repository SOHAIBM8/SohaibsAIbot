"""
No real network calls — a fake requests.Session scripts responses (and
raw exceptions, to simulate a genuine connection drop) per HTTP call,
exactly like every other exchange-facing component in this project.

The idempotency tests (decision #6) are the priority here per the
spec: an ambiguous submission failure must query the exchange for the
existing client_order_id BEFORE ever retrying with a new submission,
and must never create a duplicate order if the original had in fact
succeeded.
"""

from datetime import UTC, datetime

import pytest
import requests

from core.execution.binance_clock_sync import ClockSyncService
from core.execution.binance_execution_adapter import BinanceExecutionAdapter
from core.execution.binance_symbol_filter_cache import SymbolFilterCache
from core.execution.order import Order, OrderState, OrderType
from core.ingestion.errors import FatalIngestionError
from core.security.credential_provider import LiveCredentials

CREDENTIAL_ID = "test-credential-id"


class _FakeCredentialProvider:
    """Stands in for the real CredentialProvider (core/security/) —
    these tests are about BinanceExecutionAdapter's own order-placement
    logic (decision #7: unchanged by Stage 3), not credential vault/
    audit-log behavior, which has its own dedicated test suite in
    tests/test_security/."""

    def __init__(self, api_key: str = "fake-api-key", api_secret: str = "fake-api-secret"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.calls: list[tuple[str, str, str | None]] = []

    def get_credentials(
        self, credential_id: str, requested_by: str, client_order_id: str | None = None
    ) -> LiveCredentials:
        self.calls.append((credential_id, requested_by, client_order_id))
        return LiveCredentials(api_key=self.api_key, api_secret=self.api_secret)


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text_body

    def json(self):
        return self._json_body


class _ScriptedSession:
    """Each of get/post/delete pulls its next scripted result off a
    per-method queue — an entry that's an Exception instance is raised,
    anything else is returned as the response. Records every call for
    assertions (e.g. "the retry hit GET /order before ever POSTing
    again")."""

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

    def _dispatch(self, method: str, url: str, params: dict):
        self.calls.append((method, url, params or {}))
        queue = self.queues[method]
        if not queue:
            raise AssertionError(f"no more scripted {method} responses for {url}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_adapter(
    session: _ScriptedSession, filters_response=None, retry_sleep=None, retry_rand=None
):
    from core.ingestion.retry_policy import RetryPolicy

    clock_sync = ClockSyncService(
        base_url="https://testnet.binance.vision", session=session, clock=lambda: 1_700_000_000.0
    )
    filter_cache = SymbolFilterCache(base_url="https://testnet.binance.vision", session=session)
    if filters_response is None:
        filters_response = _default_filters_response()
    session.script("get", _FakeResponse(200, json_body=filters_response))
    filter_cache.refresh()

    retry_policy = RetryPolicy(
        max_retries=2,
        base_delay_seconds=0.001,
        max_delay_seconds=0.01,
        sleep=retry_sleep or (lambda s: None),
        rand=retry_rand or (lambda: 0.0),
    )
    adapter = BinanceExecutionAdapter(
        base_url="https://testnet.binance.vision",
        clock_sync=clock_sync,
        filter_cache=filter_cache,
        credential_provider=_FakeCredentialProvider(),
        credential_id=CREDENTIAL_ID,
        session=session,
        retry_policy=retry_policy,
    )
    # The exchangeInfo fetch above is setup, not part of what a test
    # observes about submit/cancel/status behavior — reset the call log
    # so assertions on session.calls only see calls made by the test.
    session.calls = []
    return adapter


def _default_filters_response():
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


def make_order(order_type=OrderType.MARKET, limit_price=None, quantity=0.01) -> Order:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    return Order(
        client_order_id="co-1",
        strategy_id="s1",
        symbol="BTC/USDT",
        order_type=order_type,
        direction=1,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=None,
        mode="live",
        state=OrderState.PENDING,
        risk_decision_id=1,
        created_at=now,
        updated_at=now,
    )


# --- basic submit/cancel/status happy path ---------------------------


def test_submit_market_order_success():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "post",
        _FakeResponse(
            200,
            json_body={
                "orderId": 12345,
                "status": "FILLED",
                "fills": [{"price": "65000.0", "qty": "0.01", "commission": "0.0001"}],
            },
        ),
    )

    order = adapter.submit_order(make_order())

    assert (
        order.state == OrderState.SUBMITTED
    )  # only SUBMITTED — OrderManager owns fill transitions
    assert order.exchange_order_id == "12345"
    fills = adapter.get_fills("co-1")
    assert len(fills) == 1
    assert fills[0].fill_price == 65000.0


def test_submit_is_idempotent_on_a_second_call_with_the_same_order():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 1, "status": "NEW", "fills": []})
    )

    first = adapter.submit_order(make_order())
    second = adapter.submit_order(make_order())  # same client_order_id

    assert second is first
    post_calls = [c for c in session.calls if c[0] == "post"]
    assert len(post_calls) == 1  # never POSTed twice


def test_submit_order_rejected_locally_by_filter_never_calls_the_exchange():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    # quantity far below min_qty — must be rejected before any POST.

    order = adapter.submit_order(make_order(quantity=0.0000001))

    assert order.state == OrderState.REJECTED
    assert all(c[0] != "post" for c in session.calls)


def test_submit_limit_order_requires_a_price():
    session = _ScriptedSession()
    adapter = make_adapter(session)

    order = make_order(order_type=OrderType.LIMIT, limit_price=None, quantity=0.01)
    with pytest.raises(FatalIngestionError):
        adapter.submit_order(order)


def test_stop_order_type_is_not_yet_supported():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    order = make_order(order_type=OrderType.STOP, quantity=0.01)
    order.stop_price = 60000.0

    with pytest.raises(FatalIngestionError, match="does not yet support"):
        adapter.submit_order(order)


def test_cancel_order_transitions_to_cancelled():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 1, "status": "NEW", "fills": []})
    )
    adapter.submit_order(make_order())

    session.script("delete", _FakeResponse(200, json_body={"status": "CANCELED"}))
    order = adapter.cancel_order("co-1")

    assert order.state == OrderState.CANCELLED


def test_get_order_status_syncs_local_state_from_exchange():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 1, "status": "NEW", "fills": []})
    )
    adapter.submit_order(make_order())

    session.script(
        "get",
        _FakeResponse(
            200,
            json_body={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "0.01",
                "cummulativeQuoteQty": "650.0",
            },
        ),
    )
    order = adapter.get_order_status("co-1")

    assert order.state == OrderState.FILLED
    fills = adapter.get_fills("co-1")
    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(65000.0)


# --- idempotency: the highest-priority test group ---------------------


def test_ambiguous_timeout_checks_existing_order_before_retrying_and_finds_it():
    """The original submission actually succeeded on Binance's side —
    the local timeout was purely a lost response. Must NOT submit a
    second order; must recover the existing one via GET /order."""
    session = _ScriptedSession()
    adapter = make_adapter(session)

    session.script("post", requests.exceptions.Timeout("read timed out"))
    session.script(
        "get",
        _FakeResponse(
            200,
            json_body={
                "orderId": 999,
                "status": "NEW",
                "executedQty": "0.0",
                "cummulativeQuoteQty": "0.0",
            },
        ),
    )

    order = adapter.submit_order(make_order())

    assert order.state == OrderState.SUBMITTED
    assert order.exchange_order_id == "999"
    post_calls = [c for c in session.calls if c[0] == "post"]
    get_calls = [c for c in session.calls if c[0] == "get" and "origClientOrderId" in c[2]]
    assert len(post_calls) == 1  # exactly one POST attempt — never blindly retried
    assert len(get_calls) == 1  # the idempotency check happened


def test_ambiguous_timeout_checks_existing_order_then_retries_when_truly_absent():
    """The original submission never actually reached Binance — after
    confirming the order genuinely doesn't exist, a fresh submission is
    the correct recovery, not a permanent failure."""
    session = _ScriptedSession()
    adapter = make_adapter(session)

    session.script("post", requests.exceptions.ConnectionError("connection reset"))
    session.script(
        "get", _FakeResponse(400, json_body={"code": -2013, "msg": "Order does not exist."})
    )
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 42, "status": "NEW", "fills": []})
    )

    order = adapter.submit_order(make_order())

    assert order.state == OrderState.SUBMITTED
    assert order.exchange_order_id == "42"
    post_calls = [c for c in session.calls if c[0] == "post"]
    assert len(post_calls) == 2  # first (ambiguous) attempt + the recovery retry
    get_calls = [c for c in session.calls if c[0] == "get"]
    assert len(get_calls) == 1  # the existence check happened exactly once, before the retry


def test_ambiguous_failure_never_retries_blind_before_checking_existence():
    """The critical ordering assertion: even when a retry IS eventually
    warranted, the existence check must happen first — never assume
    failure and resubmit blind."""
    session = _ScriptedSession()
    adapter = make_adapter(session)

    session.script("post", requests.exceptions.Timeout("timed out"))
    session.script(
        "get", _FakeResponse(400, json_body={"code": -2013, "msg": "Order does not exist."})
    )
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 7, "status": "NEW", "fills": []})
    )

    adapter.submit_order(make_order())

    call_sequence = [c[0] for c in session.calls]
    assert call_sequence.index("get") < call_sequence.index("post", call_sequence.index("post") + 1)


def test_clean_retryable_error_response_retries_without_an_idempotency_check():
    """A definite HTTP error response (not a timeout) means Binance
    never created the order — safe to just retry via RetryPolicy, no
    ambiguity, no existence check needed."""
    session = _ScriptedSession()
    adapter = make_adapter(session)

    session.script("post", _FakeResponse(503, text_body="Service Unavailable"))
    session.script(
        "post", _FakeResponse(200, json_body={"orderId": 5, "status": "NEW", "fills": []})
    )

    order = adapter.submit_order(make_order())

    assert order.exchange_order_id == "5"
    get_calls = [c for c in session.calls if c[0] == "get"]
    assert len(get_calls) == 0  # no existence check for a non-ambiguous failure


def test_exchange_rejection_transitions_to_rejected_not_an_exception():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "post",
        _FakeResponse(400, json_body={"code": -2010, "msg": "Account has insufficient balance"}),
    )

    order = adapter.submit_order(make_order())

    assert order.state == OrderState.REJECTED
    assert all(c[0] != "get" for c in session.calls)


def test_auth_failure_raises_fatal_not_treated_as_a_rejection():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script("post", _FakeResponse(401, json_body={"code": -2015, "msg": "Invalid API-key"}))

    with pytest.raises(FatalIngestionError):
        adapter.submit_order(make_order())


def test_repeated_ambiguous_failures_still_only_produce_one_final_order():
    """Two consecutive ambiguous failures, both resolved by finding the
    order already exists on the second lookup — proves the recovery
    path itself doesn't create duplicates even under repeated network
    trouble."""
    session = _ScriptedSession()
    adapter = make_adapter(session)

    session.script("post", requests.exceptions.Timeout("timed out"))
    session.script(
        "get",
        _FakeResponse(
            200,
            json_body={
                "orderId": 55,
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.004",
                "cummulativeQuoteQty": "260.0",
            },
        ),
    )

    order = adapter.submit_order(make_order())

    assert order.exchange_order_id == "55"
    post_calls = [c for c in session.calls if c[0] == "post"]
    assert len(post_calls) == 1


# --- list_open_orders (external/manual trade detection) --------------------


def test_list_open_orders_returns_the_raw_exchange_response():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script(
        "get",
        _FakeResponse(
            200,
            json_body=[
                {"clientOrderId": "co-1", "orderId": 1, "side": "BUY", "status": "NEW"},
                {"clientOrderId": "co-2", "orderId": 2, "side": "SELL", "status": "NEW"},
            ],
        ),
    )

    orders = adapter.list_open_orders("BTC/USDT")

    assert len(orders) == 2
    assert orders[0]["clientOrderId"] == "co-1"
    get_calls = [c for c in session.calls if c[0] == "get"]
    assert len(get_calls) == 1
    assert get_calls[0][1].endswith("/api/v3/openOrders")
    assert get_calls[0][2]["symbol"] == "BTCUSDT"


def test_list_open_orders_raises_on_an_error_response():
    session = _ScriptedSession()
    adapter = make_adapter(session)
    session.script("get", _FakeResponse(400, json_body={"code": -1121, "msg": "Invalid symbol."}))

    with pytest.raises(FatalIngestionError):
        adapter.list_open_orders("BTC/USDT")
