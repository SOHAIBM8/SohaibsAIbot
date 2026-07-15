"""
Stage 2's concrete ExecutionAdapter — replaces the Stage 1
LiveExecutionAdapter stub. OrderManager (Stage 1, unchanged) calls this
through the same ExecutionAdapter interface it already uses for
PaperExecutionAdapter (integration point #1) — nothing here required
any change to OrderManager, proving the Stage 1 interface really was
adapter-agnostic.

**Testnet only** in Stage 2 (a `CredentialProvider` pointed at a real
KMS/mainnet credential is Stage 3's business, not this file's). As of
Stage 3 (docs/execution_engine_stage3_spec.md decision #7), credentials
come from a `CredentialProvider` seam, not an environment variable
directly — see design note 6 below for exactly what changed and why.

Design notes (rule 9):

1. Ambiguous-failure idempotency (decision #6) is the core of this
   file. A connection error/timeout during order submission means we
   genuinely don't know whether Binance received and processed the
   order — `_send_new_order_once()` raises the private `_AmbiguousFailureError`
   marker for exactly that case (and only that case: a clean HTTP error
   response, even a retryable one like 429/503, means Binance
   definitely did NOT create the order, so it's just retried via
   RetryPolicy with no idempotency concern). On `_AmbiguousFailureError`,
   `submit_order()` queries the exchange for the existing
   `client_order_id` via `_lookup_existing_order()` BEFORE ever
   retrying with a fresh submission — if found, that's what actually
   happened and no duplicate is created; only if genuinely absent does
   it retry.

2. `get_order_status`/`cancel_order` need Binance's required `symbol`
   parameter, which ExecutionAdapter's interface doesn't carry
   (`client_order_id` only). This adapter keeps an in-memory
   `client_order_id -> Order` cache populated at submission time
   (mirroring PaperExecutionAdapter's `self._orders`), with an optional
   `db_session` fallback that reconstructs the full `Order` from the
   `orders` table (Stage 1's schema already has everything needed) —
   so a lookup for an order submitted before a process restart still
   works, as long as a db_session was supplied.

3. The ambiguous-failure recovery path constructs one aggregate `Fill`
   from `executedQty`/`cummulativeQuoteQty` on the recovered order
   rather than calling `GET /api/v3/myTrades` for a full per-trade
   breakdown — sufficient for `OrderManager.handle_fill()`'s
   fill-driven state transition (which doesn't need trade-level
   granularity) and avoids an extra signed call exactly in the
   failure-recovery path, where minimizing additional exchange calls
   matters most.

4. Only `MARKET` and `LIMIT` order types are implemented. `STOP`/
   `STOP_LIMIT`/`OCO` raise `FatalIngestionError` rather than being
   silently mis-mapped — their Binance parameter shapes (stopPrice,
   OCO's dual-order semantics) are real additional scope the spec's
   "highest-value step" framing doesn't ask this step to cover.

5. `get_order_status()` genuinely queries Binance (decision #4: REST
   is authoritative) and returns a SNAPSHOT copy reflecting the
   exchange's view — it never mutates the shared cached Order's state.
   See `_report_order_snapshot()`'s docstring for why: OrderManager
   holds the same Order object, and handle_fill()/ReconciliationJob
   own actual state transitions; an adapter that transitioned in place
   left them an already-terminal order and an illegal re-transition.

6. Stage 3 wiring (docs/execution_engine_stage3_spec.md decision #7 —
   confirmed explicitly with the user before making this change, since
   it touches the constructor and every public method, not a one-line
   swap): credentials are fetched via `CredentialProvider.get_credentials()`
   at the START of `submit_order()`/`cancel_order()`/`get_order_status()`
   — NOT cached as instance attributes for the adapter's lifetime, the
   way the env-var version worked. This is required, not stylistic:
   `EmergencyCredentialRevocation` (Stage 3 decision #5) must stop an
   ALREADY-CONSTRUCTED adapter from placing further orders the moment
   a credential is revoked — a construction-time-only fetch would let
   a long-lived adapter keep signing requests with a stale cached
   credential straight through a revocation, silently defeating the
   guarantee. None of the idempotency/retry/error-classification/
   state-machine logic below changed even slightly — only where the
   credential value comes from at the moment of signing did.
"""

import hashlib
import hmac
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime

import requests
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.execution.binance_clock_sync import ClockSyncService
from core.execution.binance_error_classifier import (
    BinanceErrorClassifier,
    ExchangeErrorClassification,
)
from core.execution.binance_symbol_filter_cache import FilterViolationError, SymbolFilterCache
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import Fill, Order, OrderState, OrderType
from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.ingestion.retry_policy import RetryPolicy
from core.security.credential_provider import CredentialProvider, LiveCredentials

logger = structlog.get_logger(__name__)

_BINANCE_STATUS_TO_ORDER_STATE = {
    "NEW": OrderState.SUBMITTED,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "PENDING_CANCEL": OrderState.PENDING_CANCEL,
    "REJECTED": OrderState.REJECTED,
    # Binance has no direct counterpart to our state machine's terminal
    # states for a time-in-force expiry — closest is CANCELLED.
    "EXPIRED": OrderState.CANCELLED,
}

_ORDER_DOES_NOT_EXIST_CODE = -2013


class _AmbiguousFailureError(Exception):
    """No HTTP response was ever received — we don't know if Binance
    processed the request. Never propagates out of this module."""


class _OrderRejectedError(Exception):
    """A definite 'rejected' classification (business rejection, e.g.
    insufficient balance) — turned into Order -> REJECTED by the
    caller, never raised past submit_order()."""

    def __init__(self, classification: ExchangeErrorClassification):
        super().__init__(classification.message)
        self.classification = classification


class BinanceExecutionAdapter(ExecutionAdapter):
    def __init__(
        self,
        base_url: str,
        clock_sync: ClockSyncService,
        filter_cache: SymbolFilterCache,
        credential_provider: CredentialProvider,
        credential_id: str,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
        recv_window_ms: int = 5000,
        retry_policy: RetryPolicy | None = None,
        db_session: Session | None = None,
    ):
        self._base_url = base_url
        self._clock_sync = clock_sync
        self._filter_cache = filter_cache
        self._credential_provider = credential_provider
        self._credential_id = credential_id
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._recv_window_ms = recv_window_ms
        self._retry_policy = retry_policy or RetryPolicy(max_retries=2)
        self._db = db_session

        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = {}

    def _fetch_credentials(
        self, requested_by: str, client_order_id: str | None = None
    ) -> LiveCredentials:
        return self._credential_provider.get_credentials(
            self._credential_id, requested_by=requested_by, client_order_id=client_order_id
        )

    # --- ExecutionAdapter interface --------------------------------

    def submit_order(self, order: Order) -> Order:
        if order.client_order_id in self._orders:
            return self._orders[order.client_order_id]

        try:
            self._filter_cache.validate(
                _binance_symbol(order.symbol), order.quantity, order.limit_price
            )
        except (FilterViolationError, KeyError) as exc:
            order.transition_to(OrderState.REJECTED, datetime.now(UTC))
            logger.warning(
                "binance_order_rejected_pre_submission",
                client_order_id=order.client_order_id,
                error=str(exc),
            )
            self._orders[order.client_order_id] = order
            return order

        credentials = self._fetch_credentials(
            "binance_execution_adapter.submit_order", client_order_id=order.client_order_id
        )

        try:
            payload = self._retry_policy.execute(
                lambda: self._send_new_order_once(order, credentials)
            )
        except _AmbiguousFailureError:
            logger.warning(
                "binance_submission_ambiguous_checking_existing",
                client_order_id=order.client_order_id,
            )
            existing = self._lookup_existing_order(order, credentials)
            if existing is not None:
                payload = existing
            else:
                payload = self._retry_policy.execute(
                    lambda: self._send_new_order_once(order, credentials)
                )
        except _OrderRejectedError as exc:
            order.transition_to(OrderState.REJECTED, datetime.now(UTC))
            logger.warning(
                "binance_order_rejected_by_exchange",
                client_order_id=order.client_order_id,
                binance_code=exc.classification.binance_code,
                message=exc.classification.message,
            )
            self._orders[order.client_order_id] = order
            return order

        order.exchange_order_id = str(payload.get("orderId", ""))
        order.transition_to(OrderState.SUBMITTED, datetime.now(UTC))
        self._orders[order.client_order_id] = order
        self._fills[order.client_order_id] = self._extract_fills(order, payload)
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        order = self._require_order(client_order_id)
        credentials = self._fetch_credentials(
            "binance_execution_adapter.cancel_order", client_order_id=client_order_id
        )
        response = self._signed_request(
            "DELETE",
            "/api/v3/order",
            {"symbol": _binance_symbol(order.symbol), "origClientOrderId": client_order_id},
            credentials,
        )
        if response.status_code >= 400:
            self._raise_for_error_response(response)
        order.transition_to(OrderState.CANCELLED, datetime.now(UTC))
        return order

    def get_order_status(self, client_order_id: str) -> Order:
        order = self._require_order(client_order_id)
        credentials = self._fetch_credentials(
            "binance_execution_adapter.get_order_status", client_order_id=client_order_id
        )
        response = self._signed_request(
            "GET",
            "/api/v3/order",
            {"symbol": _binance_symbol(order.symbol), "origClientOrderId": client_order_id},
            credentials,
        )
        if response.status_code >= 400:
            self._raise_for_error_response(response)
        return self._report_order_snapshot(order, response.json())

    def get_fills(self, client_order_id: str) -> list[Fill]:
        return list(self._fills.get(client_order_id, []))

    # --- external/manual trade detection (not part of ExecutionAdapter's
    # generic interface — ReconciliationJob only ever needs get_order_status()
    # for orders it already knows about; listing EVERY exchange-side open
    # order, including ones this process never placed, is fundamentally
    # Binance-specific and has no PaperExecutionAdapter equivalent, since
    # nothing external can create a paper order) -----------------------

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Raw GET /api/v3/openOrders response for one symbol — every
        currently-open order on the exchange, ours or not. Used by
        ExternalTradeDetectionService to find orders with no matching
        local client_order_id (docs/execution_engine_stage2_spec.md
        open decision #1)."""
        credentials = self._fetch_credentials("binance_execution_adapter.list_open_orders")
        response = self._signed_request(
            "GET", "/api/v3/openOrders", {"symbol": _binance_symbol(symbol)}, credentials
        )
        if response.status_code >= 400:
            self._raise_for_error_response(response)
        result: list[dict] = response.json()
        return result

    # --- order submission internals ---------------------------------

    def _send_new_order_once(self, order: Order, credentials: LiveCredentials) -> dict:
        params = self._build_order_params(order)
        response = self._signed_request("POST", "/api/v3/order", params, credentials)
        if response.status_code < 400:
            return dict(response.json())
        self._raise_for_error_response(response)
        raise AssertionError("unreachable")  # _raise_for_error_response always raises

    def _lookup_existing_order(self, order: Order, credentials: LiveCredentials) -> dict | None:
        response = self._signed_request(
            "GET",
            "/api/v3/order",
            {"symbol": _binance_symbol(order.symbol), "origClientOrderId": order.client_order_id},
            credentials,
        )
        if response.status_code == 200:
            return dict(response.json())
        if response.status_code == 400:
            code, _ = self._parse_error_body(response)
            if code == _ORDER_DOES_NOT_EXIST_CODE:
                return None
        # Anything else is itself ambiguous/unexpected — surface it
        # loudly rather than guessing whether the original order exists.
        self._raise_for_error_response(response)
        raise AssertionError("unreachable")

    def _build_order_params(self, order: Order) -> dict:
        params = {
            "symbol": _binance_symbol(order.symbol),
            "side": _binance_side(order.direction),
            "type": _binance_order_type(order.order_type),
            "quantity": _format_decimal(order.quantity),
            "newClientOrderId": order.client_order_id,
        }
        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise FatalIngestionError(f"LIMIT order {order.client_order_id} has no limit_price")
            params["price"] = _format_decimal(order.limit_price)
            params["timeInForce"] = "GTC"
        return params

    def _extract_fills(self, order: Order, payload: dict) -> list[Fill]:
        raw_fills = payload.get("fills")
        if raw_fills:
            return [
                Fill(
                    client_order_id=order.client_order_id,
                    fill_price=float(f["price"]),
                    quantity=float(f["qty"]),
                    fee=float(f.get("commission", 0)),
                    filled_at=datetime.now(UTC),
                    is_partial=False,
                )
                for f in raw_fills
            ]

        executed_qty = float(payload.get("executedQty", 0) or 0)
        if executed_qty <= 0:
            return []
        cumm_quote = float(payload.get("cummulativeQuoteQty", 0) or 0)
        avg_price = cumm_quote / executed_qty if executed_qty else 0.0
        is_partial = payload.get("status") != "FILLED"
        return [
            Fill(
                client_order_id=order.client_order_id,
                fill_price=avg_price,
                quantity=executed_qty,
                fee=0.0,
                filled_at=datetime.now(UTC),
                is_partial=is_partial,
            )
        ]

    def _report_order_snapshot(self, order: Order, payload: dict) -> Order:
        """Build a SNAPSHOT of the exchange's view of this order — a
        copy, never a mutation of the shared cached Order. Design note
        (rule 9, revised while building step 5): an earlier version of
        get_order_status() transitioned the shared Order in place, but
        OrderManager holds a reference to that same object, and
        OrderManager.handle_fill() is the single owner of every
        fill-driven state transition (Stage 1 decision, unchanged).
        Mutating here meant a caller like ReconciliationJob that checks
        exchange state and THEN routes a backfilled Fill through
        handle_fill() would hit an illegal FILLED -> FILLED transition
        — the adapter had already 'spent' the transition. Reporting a
        snapshot keeps decision #4's 'REST is authoritative' promise
        (the returned Order reflects exactly what Binance said) while
        leaving every actual state change to its rightful owner. The
        fills cache IS updated here (fills are facts, not transitions),
        so get_fills() serves the exchange-confirmed fills afterward."""
        binance_status = str(payload.get("status", ""))
        target_state = _BINANCE_STATUS_TO_ORDER_STATE.get(binance_status)
        if target_state is None:
            logger.warning(
                "binance_unrecognized_order_status",
                client_order_id=order.client_order_id,
                status=binance_status,
            )
            target_state = order.state
        if target_state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            self._fills[order.client_order_id] = self._extract_fills(order, payload)
        return replace(
            order,
            state=target_state,
            exchange_order_id=str(payload.get("orderId", order.exchange_order_id or "")),
            updated_at=datetime.now(UTC),
        )

    # --- signed HTTP plumbing ----------------------------------------

    def _signed_request(
        self, method: str, path: str, params: dict, credentials: LiveCredentials
    ) -> requests.Response:
        signed_params = dict(params)
        signed_params["timestamp"] = self._clock_sync.corrected_timestamp_ms()
        signed_params["recvWindow"] = self._recv_window_ms
        signed_params["signature"] = self._sign(signed_params, credentials.api_secret)

        url = f"{self._base_url}{path}"
        headers = {"X-MBX-APIKEY": credentials.api_key}
        try:
            if method == "GET":
                return self._session.get(
                    url, params=signed_params, headers=headers, timeout=self._timeout
                )
            if method == "POST":
                return self._session.post(
                    url, params=signed_params, headers=headers, timeout=self._timeout
                )
            if method == "DELETE":
                return self._session.delete(
                    url, params=signed_params, headers=headers, timeout=self._timeout
                )
            raise ValueError(f"unsupported HTTP method: {method}")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise _AmbiguousFailureError(str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            # Nothing was ever sent toward Binance (e.g. a malformed
            # request build) — not ambiguous in decision #6's sense,
            # just a bug.
            raise FatalIngestionError(f"request build failed calling {path}: {exc}") from exc

    def _sign(self, params: dict, api_secret: str) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _raise_for_error_response(self, response: requests.Response) -> None:
        code, message = self._parse_error_body(response)
        classification = BinanceErrorClassifier.classify(response.status_code, code, message)
        if classification.retryable:
            raise RetryableIngestionError(f"{classification.category}: {message} (code={code})")
        if classification.category == "rejected":
            raise _OrderRejectedError(classification)
        raise FatalIngestionError(f"{classification.category}: {message} (code={code})")

    @staticmethod
    def _parse_error_body(response: requests.Response) -> tuple[int | None, str]:
        try:
            body = response.json()
            return body.get("code"), body.get("msg", response.text[:200])
        except ValueError:
            return None, response.text[:200]

    # --- local order tracking ------------------------------------------

    def _require_order(self, client_order_id: str) -> Order:
        if client_order_id in self._orders:
            return self._orders[client_order_id]
        if self._db is not None:
            order = self._load_order_from_db(client_order_id)
            if order is not None:
                self._orders[client_order_id] = order
                return order
        raise KeyError(f"unknown client_order_id: {client_order_id}")

    def _load_order_from_db(self, client_order_id: str) -> Order | None:
        assert self._db is not None
        row = (
            self._db.execute(
                text("""
                    SELECT client_order_id, exchange_order_id, strategy_id, symbol, order_type,
                           direction, quantity, limit_price, stop_price, mode, state,
                           risk_decision_id, created_at, updated_at
                    FROM orders
                    WHERE client_order_id = :client_order_id
                    """),
                {"client_order_id": client_order_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return Order(
            client_order_id=row["client_order_id"],
            exchange_order_id=row["exchange_order_id"],
            strategy_id=row["strategy_id"],
            symbol=row["symbol"],
            order_type=OrderType(row["order_type"]),
            direction=row["direction"],
            quantity=float(row["quantity"]),
            limit_price=float(row["limit_price"]) if row["limit_price"] is not None else None,
            stop_price=float(row["stop_price"]) if row["stop_price"] is not None else None,
            mode=row["mode"],
            state=OrderState(row["state"]),
            risk_decision_id=row["risk_decision_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


def _binance_side(direction: int) -> str:
    return "BUY" if direction > 0 else "SELL"


def _binance_order_type(order_type: OrderType) -> str:
    if order_type == OrderType.MARKET:
        return "MARKET"
    if order_type == OrderType.LIMIT:
        return "LIMIT"
    raise FatalIngestionError(
        f"BinanceExecutionAdapter does not yet support order_type={order_type.value!r} — "
        "only MARKET and LIMIT are implemented in Stage 2; STOP/STOP_LIMIT/OCO need "
        "Binance-specific parameter shapes out of this step's scope."
    )


def _format_decimal(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")
