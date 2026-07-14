"""
ContextBuilder assembles the exact, grounded facts an LLM is allowed to
reason about — nothing broader, no inference, no join beyond what's
needed to answer the one subject asked about (docs/ai_assistant_spec.md
section 3). It connects via the llm_readonly role (core/ai_assistant/
readonly_db.py): every query here is executed under a Postgres role
that has no write grant on anything, so a bug in this file cannot
corrupt trading data even in principle.

Design notes (rule 9 — two spec gaps, filled in and flagged here):

1. `SignalLogEntry` and `RiskDecisionLogRow` are not defined anywhere
   else in the codebase; TradeExplanationContext/RiskDecisionContext
   reference them by name only. Defined here as plain read-shape
   dataclasses mirroring signal_log/risk_decision_log's columns exactly
   — this module is their only consumer, so there's no reason to give
   them a separate home.

2. There is no foreign key from `orders` to `signal_log` — an order
   only carries risk_decision_id, and risk_decision_log itself has no
   link back to the signal that triggered it either. build_trade_context()
   resolves the "signal" field with a best-effort match: the most
   recent signal_log row for the same strategy_id + symbol at or before
   the order's created_at. This is a heuristic, not an exact join —
   documented on the method itself so nobody mistakes it for a hard
   guarantee. If backtest/live code later adds a real link (e.g.
   orders.signal_log_id), this heuristic should be replaced, not kept
   alongside it.

3. build_daily_summary_context()'s equity_start/equity_end need a
   point-in-time equity figure, which only account_snapshots can give —
   but nothing in Stage 1 writes account_snapshots rows yet (a known,
   deliberate gap: see core/execution/order_manager.py's module
   docstring point 3, "nothing in OrderManager's spec'd responsibilities
   writes to it"). Rather than approximate with paper_accounts.current_cash
   (which is a live "right now" figure, not "equity as of the start of
   this particular past day", and would be silently wrong for any date
   other than today), this method raises LookupError when no snapshot
   covers the requested boundary. Fabricating a plausible-looking number
   here is exactly the kind of thing this component must never do —
   surfacing the gap loudly is the correct behavior until something
   (a future Stage 1/2 addition, out of this spec's scope) starts
   writing account_snapshots.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from datetime import date as date_type
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.execution.order import Fill, Order, OrderState, OrderType
from core.risk.risk_decision import LayerResult


@dataclass
class SignalLogEntry:
    id: int
    experiment_id: int | None
    symbol: str
    bar_time: datetime
    strategy_id: str
    regime: str | None
    regime_confidence: float | None
    direction: int | None
    signal_strength: float | None
    confidence: float | None
    reasons: list[str]
    rejected_reasons: list[str]
    acted_on: bool | None
    outcome: dict | None


@dataclass
class RiskDecisionLogRow:
    id: int
    experiment_id: int | None
    bar_time: datetime
    strategy_id: str
    proposed_quantity: float | None
    approved_quantity: float | None
    rejection_reason: str | None
    throttle_reasons: list[str]
    layer_results: list[LayerResult]
    risk_config_id: str | None


@dataclass
class TradeExplanationContext:
    order: Order
    fills: list[Fill]
    signal: SignalLogEntry
    risk_decision: RiskDecisionLogRow
    regime_at_entry: str


@dataclass
class RiskDecisionContext:
    decision: RiskDecisionLogRow
    layer_results: list[LayerResult]


@dataclass
class RegimeContext:
    symbol: str
    window_start: datetime
    window_end: datetime
    regime_history: list[dict]


@dataclass
class FilledOrderSummary:
    """Stands in for the spec's `Trade` in DailySummaryContext. Stage 1
    (docs/execution_engine_stage1_spec.md) has no position tracking and
    no persisted round-trip "trade" concept — core.portfolio.Trade is a
    backtest-only in-memory pairing of an entry and an exit that Stage 1
    never computes. Importing it here would mean fabricating entry/exit
    pairs that don't exist in the paper-trading data model. This
    dataclass instead reports exactly what Stage 1 actually persists:
    one filled order + its fill(s). Built in step 5."""

    client_order_id: str
    symbol: str
    direction: int
    quantity: float
    fill_price: float
    fee: float
    filled_at: datetime


@dataclass
class DailySummaryContext:
    account_id: str
    date: date_type
    trades: list[FilledOrderSummary]
    risk_decisions: list[RiskDecisionLogRow]
    equity_start: float
    equity_end: float


class ContextBuilder:
    """Connects via the llm_readonly role (core.ai_assistant.readonly_db).
    Every method pulls EXACTLY the relevant rows for the given
    id/account — no broader query, no inference, no join beyond what's
    needed to answer this one subject."""

    def __init__(self, readonly_db_session: Session):
        self.db = readonly_db_session

    def build_trade_context(self, order_id: str) -> TradeExplanationContext:
        order = self._fetch_order(order_id)
        fills = self._fetch_fills(order_id)
        risk_decision = self._fetch_risk_decision(order.risk_decision_id)
        signal = self._match_signal(order)
        regime_at_entry = signal.regime or "unknown"
        return TradeExplanationContext(
            order=order,
            fills=fills,
            signal=signal,
            risk_decision=risk_decision,
            regime_at_entry=regime_at_entry,
        )

    def build_risk_decision_context(self, decision_id: int) -> RiskDecisionContext:
        decision = self._fetch_risk_decision(decision_id)
        return RiskDecisionContext(decision=decision, layer_results=decision.layer_results)

    def build_daily_summary_context(self, account_id: str, date: date_type) -> DailySummaryContext:
        day_start = datetime.combine(date, time.min, tzinfo=UTC)
        day_end = datetime.combine(date, time.max, tzinfo=UTC)

        equity_start = self._equity_at_or_before(account_id, day_start)
        equity_end = self._equity_at_or_before(account_id, day_end)
        trades = self._fetch_filled_orders_for_account(account_id, day_start, day_end)
        risk_decisions = self.fetch_risk_decisions_for_account(account_id, day_start, day_end)

        return DailySummaryContext(
            account_id=account_id,
            date=date,
            trades=trades,
            risk_decisions=risk_decisions,
            equity_start=equity_start,
            equity_end=equity_end,
        )

    def build_regime_context(self, symbol: str, start: datetime, end: datetime) -> RegimeContext:
        rows = (
            self.db.execute(
                text("""
                    SELECT bar_time, regime, regime_confidence
                    FROM signal_log
                    WHERE symbol = :symbol AND bar_time BETWEEN :start AND :end
                    ORDER BY bar_time
                    """),
                {"symbol": symbol, "start": start, "end": end},
            )
            .mappings()
            .all()
        )
        regime_history = [
            {
                "bar_time": row["bar_time"],
                "regime": row["regime"],
                "regime_confidence": (
                    float(row["regime_confidence"])
                    if row["regime_confidence"] is not None
                    else None
                ),
            }
            for row in rows
        ]
        return RegimeContext(
            symbol=symbol, window_start=start, window_end=end, regime_history=regime_history
        )

    # --- internal helpers --------------------------------------------

    def _fetch_order(self, order_id: str) -> Order:
        row = (
            self.db.execute(
                text("""
                    SELECT client_order_id, exchange_order_id, strategy_id, symbol, order_type,
                           direction, quantity, limit_price, stop_price, mode, state,
                           risk_decision_id, created_at, updated_at
                    FROM orders
                    WHERE client_order_id = :order_id
                    """),
                {"order_id": order_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"no order with client_order_id={order_id}")
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

    def _fetch_fills(self, order_id: str) -> list[Fill]:
        rows = (
            self.db.execute(
                text("""
                    SELECT client_order_id, fill_price, quantity, fee, is_partial, filled_at
                    FROM fills
                    WHERE client_order_id = :order_id
                    ORDER BY filled_at
                    """),
                {"order_id": order_id},
            )
            .mappings()
            .all()
        )
        return [
            Fill(
                client_order_id=row["client_order_id"],
                fill_price=float(row["fill_price"]),
                quantity=float(row["quantity"]),
                fee=float(row["fee"]),
                filled_at=row["filled_at"],
                is_partial=row["is_partial"],
            )
            for row in rows
        ]

    def _fetch_risk_decision(self, decision_id: int) -> RiskDecisionLogRow:
        row = (
            self.db.execute(
                text("""
                    SELECT id, experiment_id, bar_time, strategy_id, proposed_quantity,
                           approved_quantity, rejection_reason, throttle_reasons,
                           layer_results, risk_config_id
                    FROM risk_decision_log
                    WHERE id = :decision_id
                    """),
                {"decision_id": decision_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"no risk_decision_log row with id={decision_id}")
        return self._risk_decision_from_row(row)

    @staticmethod
    def _risk_decision_from_row(row: Any) -> RiskDecisionLogRow:
        layer_results = [
            LayerResult(
                layer_name=layer["layer_name"],
                passed=layer["passed"],
                multiplier=layer["multiplier"],
                reason=layer.get("reason"),
            )
            for layer in (row["layer_results"] or [])
        ]
        return RiskDecisionLogRow(
            id=row["id"],
            experiment_id=row["experiment_id"],
            bar_time=row["bar_time"],
            strategy_id=row["strategy_id"],
            proposed_quantity=(
                float(row["proposed_quantity"]) if row["proposed_quantity"] is not None else None
            ),
            approved_quantity=(
                float(row["approved_quantity"]) if row["approved_quantity"] is not None else None
            ),
            rejection_reason=row["rejection_reason"],
            throttle_reasons=row["throttle_reasons"] or [],
            layer_results=layer_results,
            risk_config_id=row["risk_config_id"],
        )

    def _equity_at_or_before(self, account_id: str, as_of: datetime) -> float:
        row = (
            self.db.execute(
                text("""
                    SELECT equity FROM account_snapshots
                    WHERE account_id = :account_id AND snapshot_at <= :as_of
                    ORDER BY snapshot_at DESC
                    LIMIT 1
                    """),
                {"account_id": account_id, "as_of": as_of},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LookupError(
                f"no account_snapshots row at or before {as_of} for account_id={account_id} — "
                "Stage 1 has no snapshot-writing logic yet (see module docstring note 3); "
                "equity history cannot be reconstructed until something writes account_snapshots"
            )
        return float(row["equity"])

    def _fetch_filled_orders_for_account(
        self, account_id: str, day_start: datetime, day_end: datetime
    ) -> list[FilledOrderSummary]:
        rows = (
            self.db.execute(
                text("""
                    SELECT o.client_order_id, o.symbol, o.direction,
                           f.fill_price, f.quantity, f.fee, f.filled_at
                    FROM orders o
                    JOIN fills f ON f.client_order_id = o.client_order_id
                    WHERE o.account_id = :account_id
                      AND f.filled_at BETWEEN :day_start AND :day_end
                    ORDER BY f.filled_at
                    """),
                {"account_id": account_id, "day_start": day_start, "day_end": day_end},
            )
            .mappings()
            .all()
        )
        return [
            FilledOrderSummary(
                client_order_id=row["client_order_id"],
                symbol=row["symbol"],
                direction=row["direction"],
                quantity=float(row["quantity"]),
                fill_price=float(row["fill_price"]),
                fee=float(row["fee"]),
                filled_at=row["filled_at"],
            )
            for row in rows
        ]

    def fetch_risk_decisions_for_account(
        self, account_id: str, day_start: datetime, day_end: datetime
    ) -> list[RiskDecisionLogRow]:
        """Public (not just an internal helper) — GetRiskDecisionsTool
        (step 7) needs this without paying for build_daily_summary_context's
        equity lookups, which can raise LookupError for account_snapshots
        gaps that have nothing to do with a "what did the risk engine
        decide" question."""
        rows = (
            self.db.execute(
                text("""
                    SELECT DISTINCT rd.id, rd.experiment_id, rd.bar_time, rd.strategy_id,
                           rd.proposed_quantity, rd.approved_quantity, rd.rejection_reason,
                           rd.throttle_reasons, rd.layer_results, rd.risk_config_id
                    FROM risk_decision_log rd
                    JOIN orders o ON o.risk_decision_id = rd.id
                    WHERE o.account_id = :account_id
                      AND o.created_at BETWEEN :day_start AND :day_end
                    ORDER BY rd.bar_time
                    """),
                {"account_id": account_id, "day_start": day_start, "day_end": day_end},
            )
            .mappings()
            .all()
        )
        return [self._risk_decision_from_row(row) for row in rows]

    def _match_signal(self, order: Order) -> SignalLogEntry:
        """Best-effort match, not an exact join — see module docstring
        note 2. Raises rather than fabricating a signal if none is
        found, since inventing grounding facts is exactly what this
        component must never do."""
        row = (
            self.db.execute(
                text("""
                    SELECT id, experiment_id, symbol, bar_time, strategy_id, regime,
                           regime_confidence, direction, signal_strength, confidence,
                           reasons, rejected_reasons, acted_on, outcome
                    FROM signal_log
                    WHERE strategy_id = :strategy_id
                      AND symbol = :symbol
                      AND bar_time <= :created_at
                    ORDER BY bar_time DESC
                    LIMIT 1
                    """),
                {
                    "strategy_id": order.strategy_id,
                    "symbol": order.symbol,
                    "created_at": order.created_at,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(
                "no signal_log entry found for strategy_id="
                f"{order.strategy_id} symbol={order.symbol} at or before "
                f"order.created_at={order.created_at}"
            )
        return SignalLogEntry(
            id=row["id"],
            experiment_id=row["experiment_id"],
            symbol=row["symbol"],
            bar_time=row["bar_time"],
            strategy_id=row["strategy_id"],
            regime=row["regime"],
            regime_confidence=(
                float(row["regime_confidence"]) if row["regime_confidence"] is not None else None
            ),
            direction=row["direction"],
            signal_strength=(
                float(row["signal_strength"]) if row["signal_strength"] is not None else None
            ),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            reasons=row["reasons"] or [],
            rejected_reasons=row["rejected_reasons"] or [],
            acted_on=row["acted_on"],
            outcome=row["outcome"],
        )
