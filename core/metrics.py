"""
Performance metrics computed from a completed backtest's trade log and
equity curve. Every metric documents its exact formula and assumptions
(periods_per_year in particular) — "Sharpe ratio" means different
things depending on annualization convention, and guessing wrong here
silently is a classic way backtests mislead people.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.portfolio import Trade


@dataclass
class PerformanceMetrics:
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float  # negative fraction, e.g. -0.23 = 23% drawdown
    cagr: float
    expectancy: float  # mean pnl per trade, in account currency
    avg_r_multiple: float | None
    exposure: float  # fraction of bars with >=1 open position
    total_trades: int


def compute_metrics(
    trades: list[Trade], equity_curve: pd.Series, periods_per_year: int
) -> PerformanceMetrics:
    total_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_rate = len(wins) / total_trades if total_trades else 0.0

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    expectancy = sum(t.pnl for t in trades) / total_trades if total_trades else 0.0

    r_multiples = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r_multiple = sum(r_multiples) / len(r_multiples) if r_multiples else None

    returns = equity_curve.pct_change().dropna()

    return PerformanceMetrics(
        win_rate=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=_sharpe(returns, periods_per_year),
        sortino_ratio=_sortino(returns, periods_per_year),
        max_drawdown=_max_drawdown(equity_curve),
        cagr=_cagr(equity_curve, periods_per_year),
        expectancy=expectancy,
        avg_r_multiple=avg_r_multiple,
        exposure=_exposure(trades, equity_curve),
        total_trades=total_trades,
    )


def _sharpe(returns: pd.Series, periods_per_year: int) -> float:
    """Mean per-period return over its std, annualized by sqrt(periods
    per year). Assumes a 0% risk-free rate — fine for crypto, where the
    risk-free baseline is rarely the relevant comparison anyway."""
    if len(returns) == 0 or returns.std() == 0:
        return 0.0
    return float((returns.mean() / returns.std()) * np.sqrt(periods_per_year))


def _sortino(returns: pd.Series, periods_per_year: int) -> float:
    """Like Sharpe, but only penalizes downside deviation — a strategy
    with big upside spikes and small, consistent losses looks better
    here than under Sharpe, which penalizes both directions equally."""
    downside = returns[returns < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    return float((returns.mean() / downside.std()) * np.sqrt(periods_per_year))


def _max_drawdown(equity_curve: pd.Series) -> float:
    """Largest peak-to-trough decline, as a negative fraction of the
    peak. Uses cummax so it's measured against the equity high-water
    mark at each point, not just the starting capital."""
    if len(equity_curve) == 0:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    return float(drawdown.min())


def _cagr(equity_curve: pd.Series, periods_per_year: int) -> float:
    if len(equity_curve) < 2:
        return 0.0
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0]
    years = len(equity_curve) / periods_per_year
    if years <= 0 or total_return <= 0:
        return 0.0
    return float(total_return ** (1 / years) - 1)


def _exposure(trades: list[Trade], equity_curve: pd.Series) -> float:
    """Fraction of bars during which at least one position was open,
    reconstructed from trade entry/exit timestamps against the equity
    curve's index. Bars where a trade's timestamps aren't found in the
    index (shouldn't happen in practice) are silently skipped rather
    than raising, since this is a reporting metric, not a correctness
    check."""
    if len(equity_curve) == 0:
        return 0.0
    index_list = list(equity_curve.index)
    position_index = {ts: i for i, ts in enumerate(index_list)}
    bars_in_position: set[int] = set()
    for t in trades:
        start = position_index.get(t.entry_time)
        end = position_index.get(t.exit_time)
        if start is not None and end is not None:
            bars_in_position.update(range(start, end + 1))
    return len(bars_in_position) / len(equity_curve)
