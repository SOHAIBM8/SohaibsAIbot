"""
The output shape of a sizing decision, independent of who produced it
— `FixedFractionSizer` and `RiskEngine` both return a `SizingDecision`,
so `BacktestEngine` (and anything downstream that logs decisions) never
needs to know which one it's talking to.
"""

from dataclasses import dataclass, field

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
