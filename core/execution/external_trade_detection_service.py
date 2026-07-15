"""
External/manual trade detection (docs/execution_engine_stage2_spec.md
open decision #1 — deferred to Stage 3 there, never actually built;
closed by CLAUDE.md's "What's NOT built yet" remediation pass).
`ReconciliationJob` is scoped to OUR OWN tracked orders (matched by
client_order_id) by design (its own module docstring, decision #1) —
this service is the other half: for every symbol this platform has
ever traded live, fetch EVERY exchange-side open order (ours or not)
and flag any whose client_order_id doesn't match a local order at all.

Binance-specific by necessity, not by choice: listing every open order
on an exchange (including ones this process never placed) has no
PaperExecutionAdapter equivalent — nothing external can create a paper
order — so this depends on `ExchangeOrderLister` (a narrow Protocol
`BinanceExecutionAdapter.list_open_orders()` satisfies), not the
generic `ExecutionAdapter` interface `ReconciliationJob` uses.

Idempotent like GapDetectionService: `_record()`'s `ON CONFLICT DO
NOTHING` only reports/publishes a genuinely NEW external_trade_log
row, via the real INSERT rowcount — an already-known external order
re-seen on a later scan doesn't re-fire every cycle.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.execution.events import ExternalTradeDetected
from core.ingestion.event_bus import EventBus

logger = structlog.get_logger(__name__)


class ExchangeOrderLister(Protocol):
    def list_open_orders(self, symbol: str) -> list[dict]: ...


@dataclass
class ExternalTradeDetectionResult:
    symbol: str
    exchange_order_id: str
    exchange_client_order_id: str
    newly_recorded: bool


class ExternalTradeDetectionService:
    def __init__(
        self,
        db: Session,
        order_lister: ExchangeOrderLister,
        exchange: str = "binance",
        event_bus: EventBus | None = None,
        interval_seconds: float = 300.0,
    ):
        self.db = db
        self.order_lister = order_lister
        self.exchange = exchange
        self.event_bus = event_bus
        self.interval_seconds = interval_seconds
        self._last_run_at: datetime | None = None

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self._last_run_at is None:
            return True
        return (now - self._last_run_at).total_seconds() >= self.interval_seconds

    def run_once(self, now: datetime | None = None) -> list[ExternalTradeDetectionResult]:
        now = now or datetime.now(UTC)
        self._last_run_at = now
        results = []
        for symbol in self._live_symbols():
            known_ids = self._known_live_client_order_ids(symbol)
            for exchange_order in self.order_lister.list_open_orders(symbol):
                client_order_id = exchange_order.get("clientOrderId")
                if client_order_id in known_ids:
                    continue
                results.append(self._handle_unmatched(symbol, exchange_order, now))
        return results

    def _handle_unmatched(
        self, symbol: str, exchange_order: dict, now: datetime
    ) -> ExternalTradeDetectionResult:
        exchange_order_id = str(exchange_order.get("orderId", ""))
        exchange_client_order_id = exchange_order.get("clientOrderId") or ""
        side = exchange_order.get("side", "")
        status = exchange_order.get("status", "")

        newly_recorded = self._record(
            symbol, exchange_order_id, exchange_client_order_id, side, status, now
        )
        if newly_recorded:
            logger.warning(
                "external_trade_detected",
                exchange=self.exchange,
                symbol=symbol,
                exchange_order_id=exchange_order_id,
                exchange_client_order_id=exchange_client_order_id,
            )
            if self.event_bus is not None:
                self.event_bus.publish(
                    ExternalTradeDetected(
                        exchange=self.exchange,
                        symbol=symbol,
                        exchange_order_id=exchange_order_id,
                        exchange_client_order_id=exchange_client_order_id,
                        side=side,
                        status=status,
                        occurred_at=now,
                    )
                )
        return ExternalTradeDetectionResult(
            symbol=symbol,
            exchange_order_id=exchange_order_id,
            exchange_client_order_id=exchange_client_order_id,
            newly_recorded=newly_recorded,
        )

    def _live_symbols(self) -> list[str]:
        rows = self.db.execute(
            text("SELECT DISTINCT symbol FROM orders WHERE mode = 'live'")
        ).scalars()
        return list(rows)

    def _known_live_client_order_ids(self, symbol: str) -> set[str]:
        rows = self.db.execute(
            text("SELECT client_order_id FROM orders WHERE mode = 'live' AND symbol = :symbol"),
            {"symbol": symbol},
        ).scalars()
        return set(rows)

    def _record(
        self,
        symbol: str,
        exchange_order_id: str,
        exchange_client_order_id: str,
        side: str,
        status: str,
        now: datetime,
    ) -> bool:
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                    INSERT INTO external_trade_log
                        (exchange, symbol, exchange_order_id, exchange_client_order_id,
                         side, status, detected_at)
                    VALUES
                        (:exchange, :symbol, :exchange_order_id, :exchange_client_order_id,
                         :side, :status, :detected_at)
                    ON CONFLICT (exchange, symbol, exchange_order_id) DO NOTHING
                    """),
                {
                    "exchange": self.exchange,
                    "symbol": symbol,
                    "exchange_order_id": exchange_order_id,
                    "exchange_client_order_id": exchange_client_order_id,
                    "side": side,
                    "status": status,
                    "detected_at": now,
                },
            ),
        )
        self.db.commit()
        return result.rowcount > 0
