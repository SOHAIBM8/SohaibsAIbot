"""
Subscribes to Binance's user data stream and normalizes 'executionReport'
events into Fill objects, calling OrderManager.handle_fill() directly —
reusing Stage 1's existing method, no new fill-handling path. The
stream is a low-latency notifier, never authoritative over REST
(decision #4) — ReconciliationJob (step 5) is the actual arbiter of
state when the two disagree; this consumer's only job is to get a
likely fill applied quickly, not to be the source of truth.

Runs on core.marketdata.WebSocketConnection's existing background-
thread/synchronous-API shape — the third use of this pattern in the
project (after PostgresEventBus and LiveMarketDataSource), not a new
one.

Design note (rule 9 — a real external API change discovered and fixed
mid-build, not a design choice): the original implementation connected
to a URL embedding a listenKey obtained via the REST endpoint
`POST /api/v3/userDataStream` (Stage 4's ListenKeyManager). That REST
flow returned a confirmed `410 Gone` against real Binance testnet
during this build — Binance deprecated listenKey-based user data
streams in April 2025, and testnet has already fully removed the old
endpoints (full removal across all environments is scheduled for
2026-02-20 per Binance's own changelog). The replacement, confirmed
against Binance's current WebSocket API docs, authenticates the stream
itself: connect directly to the WebSocket API endpoint, then send a
signed `userDataStream.subscribe.signature` request as the FIRST
message (via WebSocketConnection's new `on_open` hook, added for
exactly this need). There is no separate key to obtain, renew, or
expire — the subscription lives for the life of this WebSocket
connection. `ListenKeyManager` (core/execution/binance_listen_key_manager.py)
is consequently removed rather than kept as unused dead code
implementing a REST flow that no longer exists (rule 8).

Events arrive wrapped: `{"subscriptionId": N, "event": {...}}`, where
`event` is the same executionReport shape the old stream delivered
directly — the event-payload parsing logic itself (`_to_fill`) is
unchanged.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from core.execution.order import Fill
from core.execution.order_manager import OrderManager
from core.marketdata.websocket_connection import WebSocketConnection

logger = structlog.get_logger(__name__)


class BinanceOrderStreamConsumer:
    def __init__(
        self,
        ws_url: str,
        order_manager: OrderManager,
        api_key_env_var: str = "BINANCE_TESTNET_API_KEY",
        api_secret_env_var: str = "BINANCE_TESTNET_API_SECRET",
        heartbeat_timeout_s: float = 60.0,
        connection_factory: Callable[..., WebSocketConnection] | None = None,
        timestamp_fn: Callable[[], int] | None = None,
        request_id_fn: Callable[[], str] | None = None,
        **connection_kwargs: Any,
    ):
        self.order_manager = order_manager
        self._api_key = os.environ[api_key_env_var]
        self._api_secret = os.environ[api_secret_env_var]
        self._timestamp_ms = timestamp_fn or (lambda: int(time.time() * 1000))
        self._request_id = request_id_fn or (lambda: str(uuid.uuid4()))
        factory = connection_factory or WebSocketConnection
        self._connection = factory(
            url=ws_url,
            on_message=self._on_message,
            on_open=self._build_subscribe_message,
            heartbeat_timeout_s=heartbeat_timeout_s,
            **connection_kwargs,
        )

    def start(self) -> None:
        self._connection.connect()

    def stop(self) -> None:
        self._connection.close()

    def is_connected(self) -> bool:
        return self._connection.is_alive()

    def _build_subscribe_message(self) -> str:
        params = {"apiKey": self._api_key, "timestamp": self._timestamp_ms()}
        params["signature"] = self._sign(params)
        return json.dumps(
            {
                "id": self._request_id(),
                "method": "userDataStream.subscribe.signature",
                "params": params,
            }
        )

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _on_message(self, raw_message: str) -> None:
        try:
            payload = json.loads(raw_message)
        except (TypeError, ValueError):
            logger.warning("binance_stream_message_not_json", message=raw_message[:200])
            return

        # Two shapes arrive on this connection: the subscribe request's
        # own ack ({"id": ..., "status": 200, "result": {...}}) and
        # event frames ({"subscriptionId": N, "event": {...}}). Only
        # event frames carry anything this consumer acts on.
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        if event.get("e") != "executionReport":
            return  # the user data stream also carries account updates etc — only fills matter here
        if event.get("x") != "TRADE":
            return  # NEW/CANCELED/REJECTED/... are not fill events

        try:
            fill = self._to_fill(event)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "binance_stream_malformed_execution_report", error=str(exc), payload=event
            )
            return

        try:
            self.order_manager.handle_fill(fill)
        except KeyError:
            # An execution report for an order this OrderManager
            # instance doesn't know about (a different process/account,
            # or a process restart) — never crash the consumer over it.
            # ReconciliationJob (step 5) is the actual safety net for
            # orders this process lost track of.
            logger.warning("binance_stream_fill_for_unknown_order", client_order_id=event.get("c"))

    @staticmethod
    def _to_fill(event: dict) -> Fill:
        return Fill(
            client_order_id=event["c"],
            fill_price=float(event["L"]),
            quantity=float(event["l"]),
            fee=float(event.get("n", 0) or 0),
            filled_at=datetime.fromtimestamp(event["T"] / 1000, tz=UTC),
            is_partial=event.get("X") != "FILLED",
        )
