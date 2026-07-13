"""
Execution-specific events, published on the same EventBus the
ingestion and risk components use (core/ingestion/event_bus.py) — see
that module's EventLike Protocol. Defined independently here, same
reasoning as core/risk/events.py: no dependency on the ingestion
component beyond the transport-agnostic EventBus interface.
"""

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExecutionEvent:
    event_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OrderSubmitted(ExecutionEvent):
    client_order_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    mode: str = ""
    occurred_at: datetime | None = None
    event_type: str = "OrderSubmitted"


@dataclass(frozen=True)
class OrderFilled(ExecutionEvent):
    client_order_id: str = ""
    fill_price: float = 0.0
    quantity: float = 0.0
    is_partial: bool = False
    occurred_at: datetime | None = None
    event_type: str = "OrderFilled"


@dataclass(frozen=True)
class OrderRejected(ExecutionEvent):
    client_order_id: str = ""
    reason: str = ""
    occurred_at: datetime | None = None
    event_type: str = "OrderRejected"


@dataclass(frozen=True)
class OrderCancelled(ExecutionEvent):
    client_order_id: str = ""
    occurred_at: datetime | None = None
    event_type: str = "OrderCancelled"


@dataclass(frozen=True)
class PaperFillSimulated(ExecutionEvent):
    client_order_id: str = ""
    simulated_latency_ms: float = 0.0
    occurred_at: datetime | None = None
    event_type: str = "PaperFillSimulated"
