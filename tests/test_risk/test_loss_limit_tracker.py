from datetime import UTC, datetime, timedelta

from core.portfolio import PortfolioView, PositionView, Trade
from core.risk.loss_limit_tracker import LossLimitTracker


def make_trade(pnl: float, exit_time: datetime) -> Trade:
    return Trade(
        strategy_id="s1",
        direction=1,
        entry_time=exit_time - timedelta(hours=1),
        exit_time=exit_time,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
        fees_paid=0.0,
        pnl=pnl,
        pnl_pct=pnl / 100.0,
        r_multiple=None,
        exit_reason="manual",
        regime_at_entry="bull_trend",
    )


def make_view(
    equity: float, trades: list[Trade], open_positions: list[PositionView] | None = None
) -> PortfolioView:
    return PortfolioView(
        equity=equity, peak_equity=equity, open_positions=open_positions or [], trade_history=trades
    )


def test_no_trades_or_positions_not_breached():
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)
    view = make_view(equity=10_000.0, trades=[])
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)  # a Wednesday
    assert tracker.evaluate(view, as_of) == (False, False)


def test_daily_loss_limit_breached():
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    view = make_view(equity=9_600.0, trades=[make_trade(-400.0, as_of - timedelta(hours=1))])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    daily_breached, weekly_breached = tracker.evaluate(view, as_of)
    assert daily_breached is True  # -400 / 10,000 starting = -4%, breaches 3%
    assert weekly_breached is False  # -4% doesn't breach the 8% weekly limit


def test_daily_loss_within_limit_not_breached():
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    view = make_view(equity=9_800.0, trades=[make_trade(-200.0, as_of - timedelta(hours=1))])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    assert tracker.evaluate(view, as_of) == (False, False)  # -2%, under the 3% limit


def test_unrealized_pnl_from_open_positions_counts_toward_the_loss():
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    losing_position = PositionView(
        strategy_id="s1", direction=1, entry_price=100.0, quantity=10.0, unrealized_pnl=-500.0
    )
    view = make_view(equity=9_500.0, trades=[], open_positions=[losing_position])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    daily_breached, _ = tracker.evaluate(view, as_of)
    assert daily_breached is True  # -500 / 10,000 starting = -5%, breaches 3%


def test_weekly_loss_limit_breached_from_multiple_days_none_individually_breaching():
    monday = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)  # Monday
    tuesday = datetime(2024, 6, 4, 10, 0, tzinfo=UTC)
    wednesday_asof = datetime(2024, 6, 5, 10, 0, tzinfo=UTC)

    trades = [
        make_trade(-250.0, monday),
        make_trade(-250.0, tuesday),
        make_trade(-250.0, wednesday_asof - timedelta(hours=1)),
    ]
    view = make_view(equity=10_000.0 - 750.0, trades=trades)
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.05)

    daily_breached, weekly_breached = tracker.evaluate(view, wednesday_asof)
    assert daily_breached is False  # today's -250 alone is well under 3%
    assert weekly_breached is True  # week total -750 / 10,000 = -7.5%, breaches 5%


# --- UTC midnight boundary (spec: test the boundary itself, not just "24h apart") --


def test_trade_at_23_59_59_excluded_from_the_following_day():
    """A trade that closed at the very last second of a UTC day must
    never be attributed to the next day's loss window — this is
    exactly where an off-by-one (e.g. a strictly-less-than vs
    less-than-or-equal boundary) would hide."""
    day1_last_second = datetime(2024, 6, 4, 23, 59, 59, tzinfo=UTC)
    as_of = datetime(2024, 6, 5, 8, 0, 0, tzinfo=UTC)  # the next day

    view = make_view(equity=5_000.0, trades=[make_trade(-5_000.0, day1_last_second)])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.5)

    daily_breached, _ = tracker.evaluate(view, as_of)
    # If the prior day's trade leaked into today's window, this would
    # be a massive (and false) breach. It must not be.
    assert daily_breached is False


def test_trade_at_exactly_00_00_00_is_included_in_that_day():
    """The symmetric case: a trade at the exact start of a UTC day
    belongs to THAT day, not the previous one."""
    day2_midnight = datetime(2024, 6, 5, 0, 0, 0, tzinfo=UTC)
    as_of = datetime(2024, 6, 5, 8, 0, 0, tzinfo=UTC)  # same day

    view = make_view(equity=5_000.0, trades=[make_trade(-5_000.0, day2_midnight)])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.5)

    daily_breached, _ = tracker.evaluate(view, as_of)
    assert daily_breached is True  # correctly attributed to today, breaches the limit


def test_trade_at_sunday_23_59_59_excluded_from_the_following_week():
    sunday_last_second = datetime(2024, 6, 2, 23, 59, 59, tzinfo=UTC)  # Sunday
    as_of = datetime(2024, 6, 3, 8, 0, 0, tzinfo=UTC)  # the following Monday

    view = make_view(equity=5_000.0, trades=[make_trade(-5_000.0, sunday_last_second)])
    tracker = LossLimitTracker(daily_limit_pct=0.5, weekly_limit_pct=0.03)

    _, weekly_breached = tracker.evaluate(view, as_of)
    assert weekly_breached is False


def test_trade_at_monday_00_00_00_is_included_in_that_week():
    monday_midnight = datetime(2024, 6, 3, 0, 0, 0, tzinfo=UTC)
    as_of = datetime(2024, 6, 3, 8, 0, 0, tzinfo=UTC)

    view = make_view(equity=5_000.0, trades=[make_trade(-5_000.0, monday_midnight)])
    tracker = LossLimitTracker(daily_limit_pct=0.5, weekly_limit_pct=0.03)

    _, weekly_breached = tracker.evaluate(view, as_of)
    assert weekly_breached is True


def test_gains_never_count_as_a_breach():
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    view = make_view(equity=10_500.0, trades=[make_trade(500.0, as_of - timedelta(hours=1))])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    assert tracker.evaluate(view, as_of) == (False, False)


def test_already_negative_equity_with_a_further_loss_does_not_raise_zero_division():
    """starting_equity = equity - pnl: with pnl < 0, starting_equity is
    always >= equity, so this guard only triggers for an already
    blown-through-zero account (deep negative equity) — construct that
    directly rather than a merely-large loss, which wouldn't reach it."""
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    view = make_view(equity=-150.0, trades=[make_trade(-50.0, as_of - timedelta(hours=1))])
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    daily_breached, weekly_breached = tracker.evaluate(view, as_of)
    assert daily_breached is True
    assert weekly_breached is True


def test_zero_starting_equity_with_no_activity_is_not_a_breach():
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    view = make_view(equity=-100.0, trades=[])  # blown account, but nothing happened today
    tracker = LossLimitTracker(daily_limit_pct=0.03, weekly_limit_pct=0.08)

    assert tracker.evaluate(view, as_of) == (False, False)
