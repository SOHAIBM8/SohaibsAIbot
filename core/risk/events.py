"""
Risk-specific events, published on the same EventBus the ingestion
component uses (core/ingestion/event_bus.py) — see that module's
design note on why publish() now accepts any EventLike-shaped object,
not just IngestionEvent. Defined independently here (not importing
core.ingestion.events) so core/risk has no dependency on the ingestion
component beyond the transport-agnostic EventBus interface it already
needs.
"""

import datetime as dt
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskEvent:
    event_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RiskDecisionMade(RiskEvent):
    experiment_id: int | None = None
    strategy_id: str = ""
    bar_time: dt.datetime | None = None
    approved_quantity: float = 0.0
    rejection_reason: str | None = None
    event_type: str = "RiskDecisionMade"


@dataclass(frozen=True)
class CircuitBreakerTripped(RiskEvent):
    breaker_name: str = ""
    reason: str = ""
    occurred_at: dt.datetime | None = None
    event_type: str = "CircuitBreakerTripped"


@dataclass(frozen=True)
class CircuitBreakerCleared(RiskEvent):
    breaker_name: str = ""
    occurred_at: dt.datetime | None = None
    event_type: str = "CircuitBreakerCleared"


@dataclass(frozen=True)
class KillSwitchEngaged(RiskEvent):
    engaged_by: str = ""
    reason: str = ""
    occurred_at: dt.datetime | None = None
    event_type: str = "KillSwitchEngaged"


@dataclass(frozen=True)
class KillSwitchDisengaged(RiskEvent):
    disengaged_by: str = ""
    occurred_at: dt.datetime | None = None
    event_type: str = "KillSwitchDisengaged"


@dataclass(frozen=True)
class DailyLossLimitBreached(RiskEvent):
    date: dt.date | None = None
    realized_pnl_pct: float = 0.0
    occurred_at: dt.datetime | None = None
    event_type: str = "DailyLossLimitBreached"


@dataclass(frozen=True)
class DrawdownTierChanged(RiskEvent):
    previous_tier: int = 0
    new_tier: int = 0
    current_drawdown_pct: float = 0.0
    occurred_at: dt.datetime | None = None
    event_type: str = "DrawdownTierChanged"
