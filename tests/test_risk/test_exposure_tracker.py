import pytest

from core.portfolio import PortfolioView, PositionView
from core.risk.exposure_tracker import ExposureTracker
from core.risk.rejection_reason import RejectionReason

EQUITY = 10_000.0


def position(direction: int, entry_price: float, quantity: float) -> PositionView:
    return PositionView(
        strategy_id="s",
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        unrealized_pnl=0.0,
    )


def view(positions: list[PositionView]) -> PortfolioView:
    return PortfolioView(
        equity=EQUITY, peak_equity=EQUITY, open_positions=positions, trade_history=[]
    )


@pytest.fixture
def tracker():
    return ExposureTracker(
        max_gross_pct=1.0, max_net_pct=0.5, max_concurrent=5, max_same_symbol_directional_pct=0.2
    )


def test_no_positions_is_within_limits(tracker):
    result = tracker.evaluate(view([]), proposed_direction=1)
    assert result.within_limits is True
    assert result.reason is None
    assert result.gross_exposure_pct == 0.0
    assert result.net_exposure_pct == 0.0


def test_max_concurrent_positions_breach(tracker):
    positions = [position(1, 10.0, 1.0) for _ in range(5)]  # tiny notional, count is what matters
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is False
    assert result.reason == RejectionReason.MAX_OPEN_POSITIONS


def test_under_max_concurrent_is_fine(tracker):
    positions = [position(1, 10.0, 1.0) for _ in range(4)]
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is True


def test_gross_exposure_breach(tracker):
    # Opposite directions net to ~0 but gross (sum of magnitudes) exceeds 1.0x equity.
    positions = [position(1, 6_000.0, 1.0), position(-1, 6_000.0, 1.0)]
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is False
    assert result.reason == RejectionReason.EXPOSURE_LIMIT_EXCEEDED
    assert result.gross_exposure_pct == pytest.approx(1.2)
    assert result.net_exposure_pct == pytest.approx(0.0)


def test_net_exposure_breach_without_breaching_gross(tracker):
    # A single 0.6x-equity long: gross=0.6 (under the 1.0x cap) but
    # net=0.6 breaches the tighter 0.5x net cap.
    positions = [position(1, 6_000.0, 1.0)]
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is False
    assert result.reason == RejectionReason.EXPOSURE_LIMIT_EXCEEDED
    assert result.gross_exposure_pct == pytest.approx(0.6)
    assert result.net_exposure_pct == pytest.approx(0.6)


def test_same_symbol_directional_breach_even_though_net_is_hedged(tracker):
    """Two strategies each 0.25x long/short net to ~0 (well under the
    0.5x net cap) and 0.5x gross (under the 1.0x cap), but the
    proposed direction's own directional concentration (0.25x) still
    breaches the tighter 0.2x same-symbol directional cap — this is
    the whole point of Phase A: two same-direction bets count against
    ONE figure, not as independent, uncorrelated positions."""
    positions = [position(1, 2_500.0, 1.0), position(-1, 2_500.0, 1.0)]
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is False
    assert result.reason == RejectionReason.CORRELATION_LIMIT
    assert result.gross_exposure_pct == pytest.approx(0.5)
    assert result.net_exposure_pct == pytest.approx(0.0)


def test_same_symbol_directional_check_is_specific_to_proposed_direction(tracker):
    # Long side is over the 0.2x directional cap, short side isn't.
    positions = [position(1, 2_500.0, 1.0), position(-1, 500.0, 1.0)]
    long_result = tracker.evaluate(view(positions), proposed_direction=1)
    short_result = tracker.evaluate(view(positions), proposed_direction=-1)

    assert long_result.within_limits is False
    assert long_result.reason == RejectionReason.CORRELATION_LIMIT
    assert short_result.within_limits is True  # 0.05x is well under 0.2x


def test_within_limits_reports_accurate_percentages(tracker):
    positions = [position(1, 1_000.0, 1.0), position(-1, 500.0, 1.0)]
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.within_limits is True
    assert result.gross_exposure_pct == pytest.approx(0.15)
    assert result.net_exposure_pct == pytest.approx(0.05)


def test_max_concurrent_checked_before_exposure_limits(tracker):
    """When multiple limits are simultaneously breached, the first
    check (max_concurrent) wins — a fail-fast, deterministic order."""
    positions = [
        position(1, 6_000.0, 1.0) for _ in range(5)
    ]  # breaches concurrent AND gross AND net
    result = tracker.evaluate(view(positions), proposed_direction=1)
    assert result.reason == RejectionReason.MAX_OPEN_POSITIONS


def test_zero_equity_does_not_raise_zero_division(tracker):
    degenerate_view = PortfolioView(
        equity=0.0, peak_equity=0.0, open_positions=[position(1, 100.0, 1.0)], trade_history=[]
    )
    result = tracker.evaluate(degenerate_view, proposed_direction=1)
    assert result.gross_exposure_pct == 0.0
    assert result.net_exposure_pct == 0.0
