"""
The final arbiter of order state (decision #4: the REST poll is
authoritative over the WebSocket stream whenever they disagree). Runs
on a fixed cadence via the existing Scheduler regardless of whether
anything seemed wrong — the entire point is catching problems nothing
else noticed (a lost WebSocket message, a fill during a disconnect, a
process restart between submission and fill).

Scope (confirmed open decision #1): OUR OWN tracked orders only,
matched by client_order_id. An order on the exchange with no local
counterpart is invisible to this job — external/manual trade detection
is Stage 3.

Correction paths (rule 9 — how "corrects mismatches" maps onto the
existing single-owner rules):

- Exchange says FILLED/PARTIALLY_FILLED, local disagrees: the fills the
  adapter fetched are routed through OrderManager.handle_fill() — the
  same single fill-handling path the WebSocket consumer and paper
  trading use. handle_fill() owns the state transition, persistence,
  and account update; this job never re-implements any of that.
- Exchange says CANCELLED/REJECTED, local disagrees: there is no
  OrderManager method for an exchange-initiated cancel (cancel() SENDS
  a cancel — wrong here, the exchange already did it). The correction
  is applied directly: transition_to() on the shared Order (the state
  machine's legality check still applies) + a direct UPDATE of the
  orders row. This is the one place outside OrderManager that writes
  orders.state, deliberately: reconciliation IS the designated
  authority for exchange-confirmed corrections.
- A mismatch whose correction would require an ILLEGAL transition
  (e.g. local FILLED, exchange CANCELED — a genuine anomaly, not
  staleness) is logged with corrected=False and published as a
  mismatch, never forced. A human looks at those; code guessing would
  hide exactly the anomaly this job exists to surface.

Every check writes one reconciliation_log row, mismatch or not — a
clean check is evidence too (the ingestion_run_log "no-op runs are
logged" ethos).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.execution.events import ExchangeOrderCorrected, ExchangeOrderMismatchDetected
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import OrderState, is_legal_transition
from core.execution.order_manager import OrderManager
from core.ingestion.event_bus import EventBus

logger = structlog.get_logger(__name__)

_OPEN_STATES = [
    OrderState.SUBMITTED.value,
    OrderState.PARTIALLY_FILLED.value,
    OrderState.PENDING_CANCEL.value,
]


@dataclass
class ReconciliationResult:
    client_order_id: str
    local_state: OrderState
    exchange_state: OrderState
    mismatch: bool
    corrected: bool


class ReconciliationJob:
    def __init__(
        self,
        db: Session,
        adapter: ExecutionAdapter,
        order_manager: OrderManager,
        event_bus: EventBus | None = None,
        interval_seconds: float = 60.0,
    ):
        self.db = db
        self.adapter = adapter
        self.order_manager = order_manager
        self.event_bus = event_bus
        self.interval_seconds = interval_seconds
        self._last_run_at: datetime | None = None

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self._last_run_at is None:
            return True
        return (now - self._last_run_at).total_seconds() >= self.interval_seconds

    def run_once(self, now: datetime | None = None) -> list[ReconciliationResult]:
        now = now or datetime.now(UTC)
        self._last_run_at = now
        results = []
        for row in self._open_live_orders():
            results.append(
                self._reconcile_one(row["client_order_id"], OrderState(row["state"]), now)
            )
        return results

    def _open_live_orders(self) -> list:
        return list(
            self.db.execute(
                text("""
                    SELECT client_order_id, state FROM orders
                    WHERE mode = 'live' AND state = ANY(:open_states)
                    """),
                {"open_states": _OPEN_STATES},
            ).mappings()
        )

    def _reconcile_one(
        self, client_order_id: str, local_state: OrderState, now: datetime
    ) -> ReconciliationResult:
        snapshot = self.adapter.get_order_status(client_order_id)
        exchange_state = snapshot.state
        mismatch = exchange_state != local_state
        corrected = False

        if mismatch:
            logger.warning(
                "reconciliation_mismatch_detected",
                client_order_id=client_order_id,
                local_state=local_state.value,
                exchange_state=exchange_state.value,
            )
            self._publish(
                ExchangeOrderMismatchDetected(
                    client_order_id=client_order_id,
                    local_state=local_state.value,
                    exchange_state=exchange_state.value,
                    occurred_at=now,
                )
            )
            corrected = self._correct(client_order_id, local_state, exchange_state, now)
            if corrected:
                self._publish(
                    ExchangeOrderCorrected(
                        client_order_id=client_order_id,
                        local_state=local_state.value,
                        exchange_state=exchange_state.value,
                        occurred_at=now,
                    )
                )

        self._log_check(client_order_id, local_state, exchange_state, mismatch, corrected, now)
        return ReconciliationResult(
            client_order_id=client_order_id,
            local_state=local_state,
            exchange_state=exchange_state,
            mismatch=mismatch,
            corrected=corrected,
        )

    def _correct(
        self,
        client_order_id: str,
        local_state: OrderState,
        exchange_state: OrderState,
        now: datetime,
    ) -> bool:
        if not is_legal_transition(local_state, exchange_state):
            logger.error(
                "reconciliation_illegal_correction_left_for_review",
                client_order_id=client_order_id,
                local_state=local_state.value,
                exchange_state=exchange_state.value,
            )
            return False

        if exchange_state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            fills = self.adapter.get_fills(client_order_id)
            if not fills:
                logger.error(
                    "reconciliation_filled_but_no_fills_reported",
                    client_order_id=client_order_id,
                )
                return False
            for fill in fills:
                self.order_manager.handle_fill(fill)
            return True

        # CANCELLED/REJECTED confirmed by the exchange: apply directly
        # (see module docstring — no OrderManager method exists for an
        # exchange-initiated terminal state, and cancel() would SEND a
        # cancel, which is wrong here).
        self.db.execute(
            text("""
                UPDATE orders SET state = :state, updated_at = :updated_at
                WHERE client_order_id = :client_order_id
                """),
            {
                "state": exchange_state.value,
                "updated_at": now,
                "client_order_id": client_order_id,
            },
        )
        self.db.commit()
        return True

    def _log_check(
        self,
        client_order_id: str,
        local_state: OrderState,
        exchange_state: OrderState,
        mismatch: bool,
        corrected: bool,
        now: datetime,
    ) -> None:
        self.db.execute(
            text("""
                INSERT INTO reconciliation_log
                    (client_order_id, local_state, exchange_state, mismatch, corrected, checked_at)
                VALUES
                    (:client_order_id, :local_state, :exchange_state,
                     :mismatch, :corrected, :checked_at)
                """),
            {
                "client_order_id": client_order_id,
                "local_state": local_state.value,
                "exchange_state": exchange_state.value,
                "mismatch": mismatch,
                "corrected": corrected,
                "checked_at": now,
            },
        )
        self.db.commit()

    def _publish(self, event: object) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event)  # type: ignore[arg-type]
