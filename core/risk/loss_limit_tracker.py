"""
LossLimitTracker (spec decision #1): UTC daily (00:00-23:59) and
weekly (Mon 00:00 - Sun 23:59) realized+unrealized PnL, computed
directly from PortfolioView — not a separate ledger, so there's only
ever one source of truth for "what happened" (Portfolio's trade
history and open positions), never a second bookkeeping system that
can drift out of sync with it.

Percentage basis: PnL is expressed as a fraction of equity BEFORE that
period's PnL was applied (current equity minus the period's PnL),
i.e. "how much of your starting-of-period capital did you lose" — not
a fraction of the already-shrunk current equity, which would
understate the loss.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.portfolio import PortfolioView


@dataclass
class _PeriodPnl:
    pnl: float
    pct: float  # negative = loss, as a fraction of starting-of-period equity


class LossLimitTracker:
    def __init__(self, daily_limit_pct: float, weekly_limit_pct: float):
        self.daily_limit_pct = daily_limit_pct
        self.weekly_limit_pct = weekly_limit_pct

    def evaluate(self, portfolio_view: PortfolioView, as_of: datetime) -> tuple[bool, bool]:
        """Returns (daily_breached, weekly_breached)."""
        daily = self._period_pnl(portfolio_view, self._start_of_day(as_of), as_of)
        weekly = self._period_pnl(portfolio_view, self._start_of_week(as_of), as_of)
        daily_breached = daily.pct <= -self.daily_limit_pct
        weekly_breached = weekly.pct <= -self.weekly_limit_pct
        return daily_breached, weekly_breached

    def _period_pnl(
        self, portfolio_view: PortfolioView, window_start: datetime, as_of: datetime
    ) -> _PeriodPnl:
        realized = sum(
            t.pnl for t in portfolio_view.trade_history if window_start <= t.exit_time <= as_of
        )
        unrealized = sum(p.unrealized_pnl for p in portfolio_view.open_positions)
        pnl = realized + unrealized

        starting_equity = portfolio_view.equity - pnl
        if starting_equity <= 0:
            # Capital already wiped out — any further loss is a breach,
            # any gain (or flat) isn't. Avoids a ZeroDivisionError
            # without silently reporting 0% on a blown-up account.
            pct = -1.0 if pnl < 0 else 0.0
        else:
            pct = pnl / starting_equity
        return _PeriodPnl(pnl=pnl, pct=pct)

    @staticmethod
    def _start_of_day(as_of: datetime) -> datetime:
        return as_of.replace(hour=0, minute=0, second=0, microsecond=0)

    @classmethod
    def _start_of_week(cls, as_of: datetime) -> datetime:
        start_of_day = cls._start_of_day(as_of)
        return start_of_day - timedelta(days=as_of.weekday())  # Monday = 0
