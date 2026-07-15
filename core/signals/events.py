"""
Signal-scanner events, published on the same EventBus the ingestion,
risk, and execution components use (core/ingestion/event_bus.py) — see
that module's EventLike Protocol. Same independent-module pattern as
core/execution/events.py and core/risk/events.py: no dependency on
any other component beyond the transport-agnostic EventBus interface.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SignalEvent:
    event_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TradeSignalGenerated(SignalEvent):
    """A strategy produced a directional (non-flat) signal on this
    scan. Signal-only by design (core/signals/signal_scanner.py never
    calls OrderManager) — this event reports an observation, never an
    order placed."""

    strategy_id: str = ""
    symbol: str = ""
    direction: int = 0  # 1 long, -1 short
    signal_strength: float = 0.0
    confidence: float | None = None
    regime_trend: str = ""
    regime_vol: str = ""
    reasons: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None
    event_type: str = "TradeSignalGenerated"
