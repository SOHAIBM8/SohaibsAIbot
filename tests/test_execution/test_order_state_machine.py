"""
Exhaustive state machine tests — the highest-value test file in the
whole spec, since Stage 2/3 both depend on this being correct. Every
legal transition must succeed; every illegal transition must raise.
Tested over the full (from, to) cross product, not just a hand-picked
subset, so a future edit to _LEGAL_TRANSITIONS can't silently open up
an unintended path without a test catching it.
"""

import itertools
from datetime import UTC, datetime

import pytest

from core.execution.order import Order, OrderState, OrderType, is_legal_transition

LEGAL_PAIRS = {
    (OrderState.PENDING, OrderState.SUBMITTED),
    (OrderState.PENDING, OrderState.REJECTED),
    (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED),
    (OrderState.SUBMITTED, OrderState.FILLED),
    (OrderState.SUBMITTED, OrderState.PENDING_CANCEL),
    (OrderState.SUBMITTED, OrderState.CANCELLED),
    (OrderState.SUBMITTED, OrderState.REJECTED),
    (OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED),
    (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
    (OrderState.PARTIALLY_FILLED, OrderState.PENDING_CANCEL),
    (OrderState.PARTIALLY_FILLED, OrderState.CANCELLED),
    (OrderState.PENDING_CANCEL, OrderState.CANCELLED),
    (OrderState.PENDING_CANCEL, OrderState.FILLED),
    (OrderState.PENDING_CANCEL, OrderState.PARTIALLY_FILLED),
}

ALL_PAIRS = list(itertools.product(OrderState, OrderState))
ILLEGAL_PAIRS = [pair for pair in ALL_PAIRS if pair not in LEGAL_PAIRS]

TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}


def make_order(state: OrderState) -> Order:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    return Order(
        client_order_id="co-1",
        strategy_id="s1",
        symbol="BTC/USDT",
        order_type=OrderType.MARKET,
        direction=1,
        quantity=1.0,
        limit_price=None,
        stop_price=None,
        mode="paper",
        state=state,
        risk_decision_id=1,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    "from_state,to_state", sorted(LEGAL_PAIRS, key=lambda p: (p[0].value, p[1].value))
)
def test_legal_transition_succeeds(from_state, to_state):
    order = make_order(from_state)
    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)

    order.transition_to(to_state, as_of)

    assert order.state == to_state
    assert order.updated_at == as_of


@pytest.mark.parametrize(
    "from_state,to_state", sorted(ILLEGAL_PAIRS, key=lambda p: (p[0].value, p[1].value))
)
def test_illegal_transition_raises(from_state, to_state):
    order = make_order(from_state)
    original_updated_at = order.updated_at

    with pytest.raises(ValueError, match="illegal order state transition"):
        order.transition_to(to_state, datetime(2024, 6, 1, 12, 0, tzinfo=UTC))

    # A rejected transition must never partially apply.
    assert order.state == from_state
    assert order.updated_at == original_updated_at


def test_every_cross_product_pair_is_covered_by_exactly_one_expectation():
    # Guards the test file itself: if OrderState ever gains/loses a
    # member, this fails loudly instead of silently under-testing.
    assert len(LEGAL_PAIRS) + len(ILLEGAL_PAIRS) == len(OrderState) ** 2
    assert LEGAL_PAIRS.isdisjoint(ILLEGAL_PAIRS)


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_legal_outgoing_transitions(terminal_state):
    for other_state in OrderState:
        assert is_legal_transition(terminal_state, other_state) is False


def test_no_state_can_transition_to_pending():
    # PENDING is the only entry point (set at Order construction, never
    # transitioned into) — nothing should ever transition back to it.
    for state in OrderState:
        assert is_legal_transition(state, OrderState.PENDING) is False
