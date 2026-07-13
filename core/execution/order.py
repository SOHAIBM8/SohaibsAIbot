"""
Order, Fill, and the order state machine (spec decision #1: identical
for paper and live — only the ExecutionAdapter implementation
differs). `Order.transition_to()` is the single choke point every
state change must pass through; OrderManager (step 4) calls it, never
sets `order.state` directly. Illegal transitions raise rather than
silently clamping or ignoring — a state machine that quietly accepts
an impossible transition (e.g. FILLED -> SUBMITTED) is worse than one
that crashes loudly, because it hides the bug that got you there.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OrderState(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    OCO = "oco"


# Legal next-states per current state. FILLED/CANCELLED/REJECTED are
# terminal — nothing transitions out of them. PARTIALLY_FILLED ->
# PARTIALLY_FILLED is legal (another partial fill arrives).
# PENDING_CANCEL can still resolve to FILLED or PARTIALLY_FILLED — a
# cancel request racing with an in-flight fill is a real exchange
# behavior, not a bug, so the state machine must allow it rather than
# forcing every cancel to "win."
_LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING: frozenset({OrderState.SUBMITTED, OrderState.REJECTED}),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.PENDING_CANCEL,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.PENDING_CANCEL,
            OrderState.CANCELLED,
        }
    ),
    OrderState.PENDING_CANCEL: frozenset(
        {
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


def is_legal_transition(from_state: OrderState, to_state: OrderState) -> bool:
    return to_state in _LEGAL_TRANSITIONS[from_state]


@dataclass
class Order:
    client_order_id: str  # generated locally, BEFORE any adapter call
    strategy_id: str
    symbol: str
    order_type: OrderType
    direction: int  # 1 buy, -1 sell
    quantity: float
    limit_price: float | None
    stop_price: float | None
    mode: str  # 'paper' | 'live'
    state: OrderState
    risk_decision_id: int  # FK to risk_decision_log — required, not optional
    created_at: datetime
    updated_at: datetime
    exchange_order_id: str | None = None  # null until Stage 2 assigns one

    def transition_to(self, new_state: OrderState, as_of: datetime) -> None:
        if not is_legal_transition(self.state, new_state):
            raise ValueError(
                f"illegal order state transition: {self.state.value} -> {new_state.value} "
                f"(client_order_id={self.client_order_id})"
            )
        self.state = new_state
        self.updated_at = as_of


@dataclass
class Fill:
    client_order_id: str
    fill_price: float
    quantity: float
    fee: float
    filled_at: datetime
    is_partial: bool
