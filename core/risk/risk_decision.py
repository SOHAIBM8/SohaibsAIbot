"""
The output shape of a sizing decision, independent of who produced it
— `FixedFractionSizer` and `RiskEngine` both return a `SizingDecision`,
so `BacktestEngine` (and anything downstream that logs decisions) never
needs to know which one it's talking to.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from core.risk.rejection_reason import RejectionReason, ThrottleReason


@dataclass
class LayerResult:
    layer_name: str
    passed: bool
    multiplier: float  # 1.0 if this layer applied no throttle
    reason: str | None = None


@dataclass
class SizingDecision:
    approved_quantity: float  # 0.0 if fully vetoed
    proposed_quantity: float  # pre-cap quantity from the sizing method, for audit
    rejection_reason: RejectionReason | None = None
    throttle_reasons: list[ThrottleReason] = field(default_factory=list)
    layer_results: list[LayerResult] = field(default_factory=list)
    # The risk_decision_log row id this decision was persisted as, if
    # any. Added for docs/execution_engine_stage1_spec.md: Order.
    # risk_decision_id is a required (not optional) FK into
    # risk_decision_log, and OrderManager.submit() takes only a
    # SizingDecision — the id has to travel on the decision itself.
    # None for sizers that never persist a decision (FixedFractionSizer,
    # test doubles) — OrderManager must reject those, never guess an id.
    risk_decision_id: int | None = None


@dataclass
class RiskDecisionRecord:
    """A persisted risk_decision_log row, read back — added for the
    dashboard's Risk monitoring page. This is the only externally
    observable trace of what DrawdownMonitor/ExposureTracker/
    LossLimitTracker actually decided at the moment of a real sizing
    call, since those calculators are themselves stateless and take no
    db handle (see their own module docstrings) — an API process has
    no other way to know "what did the risk engine last decide."""

    id: int
    experiment_id: int | None
    bar_time: datetime
    strategy_id: str
    proposed_quantity: float
    approved_quantity: float
    rejection_reason: RejectionReason | None
    throttle_reasons: list[ThrottleReason]
    layer_results: list[LayerResult]
    risk_config_id: str | None


class RiskDecisionLogReader:
    """Read-only access to risk_decision_log. A separate class from
    RiskEngine (which only ever INSERTs, via _log_decision) — nothing
    about reading past decisions belongs on the class responsible for
    making new ones."""

    def __init__(self, db: Session):
        self.db = db

    def list_recent(self, limit: int = 20) -> list[RiskDecisionRecord]:
        rows = (
            self.db.execute(
                text("""
                    SELECT id, experiment_id, bar_time, strategy_id, proposed_quantity,
                           approved_quantity, rejection_reason, throttle_reasons,
                           layer_results, risk_config_id
                    FROM risk_decision_log
                    ORDER BY bar_time DESC
                    LIMIT :limit
                    """),
                {"limit": limit},
            )
            .mappings()
            .all()
        )
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: RowMapping) -> RiskDecisionRecord:
        layer_results = [
            LayerResult(
                layer_name=layer["layer_name"],
                passed=layer["passed"],
                multiplier=layer["multiplier"],
                reason=layer.get("reason"),
            )
            for layer in (row["layer_results"] or [])
        ]
        throttle_reasons = [ThrottleReason(t) for t in (row["throttle_reasons"] or [])]
        rejection_reason = (
            RejectionReason(row["rejection_reason"]) if row["rejection_reason"] else None
        )
        return RiskDecisionRecord(
            id=row["id"],
            experiment_id=row["experiment_id"],
            bar_time=row["bar_time"],
            strategy_id=row["strategy_id"],
            proposed_quantity=float(row["proposed_quantity"]),
            approved_quantity=float(row["approved_quantity"]),
            rejection_reason=rejection_reason,
            throttle_reasons=throttle_reasons,
            layer_results=layer_results,
            risk_config_id=row["risk_config_id"],
        )
