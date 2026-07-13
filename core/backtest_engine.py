"""
Event-driven backtest loop. Single-symbol in V1 — multi-symbol
portfolios are a natural later extension but add position-sizing/
exposure complexity that belongs with the Risk Engine, not this
component.

Per bar, in this exact order:

  1. Execute any entries QUEUED from the previous bar, filled at THIS
     bar's open. This is the fix for same-bar lookahead: a strategy
     decides "buy" using bar T's completed close price, but that
     decision can only be acted on starting at bar T+1's open — you
     can't know bar T closed at that price until it already has,
     by which point bar T+1 is forming. Filling at bar T's own close
     (which many simple backtest tutorials do) silently assumes
     zero-latency execution.

  2. Resolve exits (stop-loss / take-profit) using THIS bar's high/low.
     If both a stop and a target are hit within the same bar, the stop
     is assumed to trigger first — a conservative assumption, since we
     can't know the intrabar path from OHLC data alone.

  3. Classify regime and generate new signals from strategies eligible
     for that regime, using this bar's now-complete feature values.
     New entries are QUEUED for execution at the NEXT bar's open (see
     step 1), not filled immediately.

  4. Mark the portfolio to market at this bar's close.

Bars before the feature warmup period (NaN in any feature any active
strategy or the regime detector needs) are skipped for steps 2-3, but
still marked to market, so the equity curve has no timestamp gaps.
"""

from dataclasses import dataclass, field

import pandas as pd

from core.feature_store import FeatureWindow
from core.portfolio import Portfolio, Trade
from core.position_sizing import PositionSizer
from core.regime_detector import RegimeDetector
from core.strategy_base import Signal, StrategyBase
from core.execution_model import ExecutionModel

REGIME_REQUIRED_FEATURES = ["ema_20", "ema_50", "adx_14", "atr_percentile_90"]


@dataclass
class SignalLogEntry:
    bar_time: object
    strategy_id: str
    regime_trend: str
    regime_vol: str
    direction: int
    signal_strength: float
    reasons: list[str]
    rejected_reasons: list[str]
    acted_on: bool


@dataclass
class _PendingEntry:
    strategy_id: str
    signal: Signal
    window: FeatureWindow
    regime_at_entry: str


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    signal_log: list[SignalLogEntry] = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        strategies: list[StrategyBase],
        regime_detector: RegimeDetector,
        position_sizer: PositionSizer,
        execution_model: ExecutionModel,
        initial_capital: float = 10_000.0,
    ):
        self.strategies = strategies
        self.regime_detector = regime_detector
        self.position_sizer = position_sizer
        self.execution_model = execution_model
        self.initial_capital = initial_capital
        self._required_features = sorted(set(
            REGIME_REQUIRED_FEATURES
            + [f for s in strategies for f in s.required_features if f not in ("close", "open", "high", "low")]
        ))

    def run(self, feature_df: pd.DataFrame) -> BacktestResult:
        self.regime_detector.reset()
        portfolio = Portfolio(self.initial_capital, self.execution_model)
        signal_log: list[SignalLogEntry] = []
        pending_entries: dict[str, _PendingEntry] = {}

        rows = list(feature_df.iterrows())
        for i, (timestamp, row) in enumerate(rows):
            # 1. execute entries queued from the previous bar, at this bar's open
            for strategy_id, pending in pending_entries.items():
                if strategy_id in portfolio.open_positions:
                    continue  # a position opened another way in the meantime
                quantity = self.position_sizer.size(pending.signal, portfolio.cash, pending.window)
                if quantity > 0:
                    portfolio.open_position(
                        strategy_id=strategy_id, direction=pending.signal.direction,
                        reference_price=row["open"], quantity=quantity, entry_time=timestamp,
                        stop_loss=pending.signal.stop_loss, take_profit=pending.signal.take_profit,
                        regime_at_entry=pending.regime_at_entry,
                    )
            pending_entries = {}

            # 2. resolve exits using this bar's high/low
            self._process_exits(portfolio, row, timestamp)

            # warmup check: skip regime/signal generation until required
            # features are all non-NaN, but still mark to market below
            warmed_up = not row[self._required_features].isna().any() if self._required_features else True

            if warmed_up:
                window = FeatureWindow(symbol="", timeframe="", as_of=timestamp, values=row.to_dict())
                regime_state = self.regime_detector.classify(window)
                eligible_ids = {
                    s.meta.strategy_id for s in self.strategies
                    if regime_state.trend in s.meta.works_best_in
                    and (not s.meta.works_best_in_vol or regime_state.vol in s.meta.works_best_in_vol)
                }

                for strategy in self.strategies:
                    is_eligible = strategy.meta.strategy_id in eligible_ids
                    if not is_eligible:
                        signal_log.append(SignalLogEntry(
                            bar_time=timestamp, strategy_id=strategy.meta.strategy_id,
                            regime_trend=regime_state.trend.value, regime_vol=regime_state.vol.value,
                            direction=0, signal_strength=0.0, reasons=[],
                            rejected_reasons=["not eligible for current regime"], acted_on=False,
                        ))
                        continue

                    signal = strategy.generate_signal(window)
                    acted_on = False
                    already_open = strategy.meta.strategy_id in portfolio.open_positions
                    if signal.direction != 0 and not already_open and i + 1 < len(rows):
                        pending_entries[strategy.meta.strategy_id] = _PendingEntry(
                            strategy_id=strategy.meta.strategy_id, signal=signal,
                            window=window, regime_at_entry=regime_state.trend.value,
                        )
                        acted_on = True  # queued, not yet filled

                    signal_log.append(SignalLogEntry(
                        bar_time=timestamp, strategy_id=strategy.meta.strategy_id,
                        regime_trend=regime_state.trend.value, regime_vol=regime_state.vol.value,
                        direction=signal.direction, signal_strength=signal.signal_strength,
                        reasons=signal.reasons, rejected_reasons=signal.rejected_reasons,
                        acted_on=acted_on,
                    ))

            # 3. mark to market at this bar's close, warmup bars included
            portfolio.mark_to_market(timestamp, row["close"])

        self._close_all_at_end(portfolio, rows)

        equity_series = pd.Series(
            [e for _, e in portfolio.equity_curve],
            index=[t for t, _ in portfolio.equity_curve],
            name="equity",
        )
        return BacktestResult(trades=portfolio.trades, equity_curve=equity_series, signal_log=signal_log)

    def _process_exits(self, portfolio: Portfolio, row, timestamp) -> None:
        for strategy_id in list(portfolio.open_positions.keys()):
            pos = portfolio.open_positions[strategy_id]
            high, low = row["high"], row["low"]
            hit_stop = pos.stop_loss is not None and (
                (pos.direction > 0 and low <= pos.stop_loss)
                or (pos.direction < 0 and high >= pos.stop_loss)
            )
            hit_target = pos.take_profit is not None and (
                (pos.direction > 0 and high >= pos.take_profit)
                or (pos.direction < 0 and low <= pos.take_profit)
            )
            if hit_stop:
                portfolio.close_position(strategy_id, pos.stop_loss, timestamp, "stop_loss")
            elif hit_target:
                portfolio.close_position(strategy_id, pos.take_profit, timestamp, "take_profit")

    def _close_all_at_end(self, portfolio: Portfolio, rows) -> None:
        if portfolio.open_positions and rows:
            last_time, last_row = rows[-1]
            for strategy_id in list(portfolio.open_positions.keys()):
                portfolio.close_position(strategy_id, last_row["close"], last_time, "end_of_backtest")
