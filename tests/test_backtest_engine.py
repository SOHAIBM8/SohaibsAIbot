import pandas as pd
import pytest

from core.backtest_engine import BacktestEngine
from core.execution_model import ExecutionModel
from core.position_sizing import PositionSizer
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.risk.risk_decision import SizingDecision
from core.strategy_base import Regime, Signal, StrategyBase, StrategyMeta


class FixedQuantitySizer(PositionSizer):
    """Deterministic sizer for tests — always trades a fixed quantity,
    so PnL arithmetic is exactly checkable by hand."""

    def __init__(self, quantity=10.0):
        self.quantity = quantity

    def size(self, signal, context):
        return SizingDecision(approved_quantity=self.quantity, proposed_quantity=self.quantity)


def make_strategy(
    name, trigger_column, direction=1, stop_loss=None, take_profit=None, regime=Regime.SIDEWAYS
):
    """Builds a minimal StrategyBase subclass whose only logic is:
    'fire once when trigger_column == 1'. Used across tests instead of
    the real reference strategies so each test isolates exactly the
    engine behavior it's checking."""

    class _TestStrategy(StrategyBase):
        meta = StrategyMeta(
            name=name,
            version="1.0.0",
            author="test",
            created_at=None,
            description="test fixture",
            parameters={},
            compatible_pipeline_versions=["features_v1"],
            works_best_in=[regime],
        )
        required_features = [trigger_column]
        min_lookback = 0

        def generate_signal(self, feature_window) -> Signal:
            if feature_window.get(trigger_column) == 1:
                return Signal(
                    direction=direction,
                    entry_price=feature_window.get("close"),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    strategy_id=self.meta.strategy_id,
                    signal_strength=1.0,
                    reasons=["test trigger fired"],
                )
            return Signal(
                direction=0,
                entry_price=feature_window.get("close"),
                stop_loss=None,
                take_profit=None,
                strategy_id=self.meta.strategy_id,
                signal_strength=0.0,
                rejected_reasons=["trigger not set"],
            )

        def validate(self, feature_registry):
            return []

    return _TestStrategy()


def base_columns(n, trigger_col="buy_trigger", trigger_bar=None):
    """Baseline OHLC + regime feature columns, rising prices. adx_14
    held below threshold (forces SIDEWAYS, matching the detector's
    default confirmed state so no hysteresis delay is needed) and
    atr_percentile_90 mid-range (NORMAL_VOL, also the default) — so
    strategies are eligible from bar 0.

    NOTE: open/close deliberately do NOT satisfy open[i+1] == close[i]
    — an earlier version of this fixture did, by coincidence of the
    arithmetic, which meant the same-bar-close-vs-next-bar-open
    lookahead test couldn't actually distinguish the two cases."""
    open_ = [100.0 + 3 * i for i in range(n)]
    df = pd.DataFrame(
        {
            "open": open_,
            "high": [o + 2 for o in open_],
            "low": [o - 1 for o in open_],
            "close": [o + 1 for o in open_],
            "ema_20": [100.0] * n,
            "ema_50": [100.0] * n,
            "adx_14": [10.0] * n,  # below threshold -> SIDEWAYS
            "atr_percentile_90": [0.5] * n,  # mid-range -> NORMAL_VOL
            trigger_col: [0] * n,
        }
    )
    if trigger_bar is not None:
        df.loc[trigger_bar, trigger_col] = 1
    return df


def declining_columns(n, trigger_col="buy_trigger", trigger_bar=None):
    """Same shape as base_columns but with a falling price series —
    for tests where a short position needs to actually be profitable."""
    open_ = [100.0 - 3 * i for i in range(n)]
    df = pd.DataFrame(
        {
            "open": open_,
            "high": [o + 1 for o in open_],
            "low": [o - 2 for o in open_],
            "close": [o - 1 for o in open_],
            "ema_20": [100.0] * n,
            "ema_50": [100.0] * n,
            "adx_14": [10.0] * n,
            "atr_percentile_90": [0.5] * n,
            trigger_col: [0] * n,
        }
    )
    if trigger_bar is not None:
        df.loc[trigger_bar, trigger_col] = 1
    return df


def make_engine(strategies, quantity=10.0, fee_bps=0.0, slippage_bps=0.0):
    return BacktestEngine(
        strategies=strategies,
        regime_detector=RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1)),
        position_sizer=FixedQuantitySizer(quantity),
        execution_model=ExecutionModel(fee_bps=fee_bps, slippage_bps=slippage_bps),
        initial_capital=10_000.0,
    )


# --- the lookahead fix: this is the most important test in this file -------


def test_signal_executes_at_next_bar_open_not_signal_bar_close():
    """Trigger fires on bar 3. bar3: open=109, close=110. bar4: open=112,
    close=113. The fix under test: the fill must use bar 4's open (112),
    never bar 3's close (110) and never bar 4's close (113)."""
    df = base_columns(n=6, trigger_bar=3)
    strategy = make_strategy("lookahead_probe", "buy_trigger")
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(112.0)  # bar 4's open
    assert trade.entry_price != pytest.approx(110.0)  # NOT bar 3's close (the bug)
    assert trade.entry_price != pytest.approx(113.0)  # NOT bar 4's close (also wrong)


def test_no_trade_possible_if_trigger_is_on_the_last_bar():
    """A signal on the final bar has no next bar to execute on — it
    should simply never fill, not crash and not fill on the same bar."""
    df = base_columns(n=5, trigger_bar=4)
    strategy = make_strategy("last_bar_probe", "buy_trigger")
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 0


# --- warmup handling --------------------------------------------------------


def test_trigger_during_warmup_produces_no_trade():
    df = base_columns(n=6, trigger_bar=1)
    df.loc[0:2, "adx_14"] = float("nan")  # bars 0-2 still warming up
    strategy = make_strategy("warmup_probe", "buy_trigger")
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 0  # trigger was during warmup, correctly ignored


def test_equity_curve_has_no_gaps_during_warmup():
    df = base_columns(n=6)
    df.loc[0:2, "adx_14"] = float("nan")
    strategy = make_strategy("warmup_probe2", "buy_trigger")
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.equity_curve) == len(df)  # every bar marked to market regardless


# --- long / short round trips with stop-loss / take-profit -----------------


def test_long_position_exits_on_take_profit():
    df = base_columns(n=8, trigger_bar=1)
    # bar 2's open is the fill price (102); set take_profit reachable by bar 4's high
    strategy = make_strategy(
        "tp_probe", "buy_trigger", direction=1, stop_loss=95.0, take_profit=108.0
    )
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].pnl > 0


def test_long_position_exits_on_stop_loss():
    df = base_columns(n=8, trigger_bar=1)
    df["low"] = [x - 20 for x in df["low"]]  # push lows down so the stop gets hit
    strategy = make_strategy(
        "sl_probe", "buy_trigger", direction=1, stop_loss=90.0, take_profit=500.0
    )
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].pnl < 0


def test_short_position_profits_when_price_falls():
    df = declining_columns(n=8, trigger_bar=1)
    strategy = make_strategy(
        "short_probe", "buy_trigger", direction=-1, stop_loss=200.0, take_profit=None
    )
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 1
    assert result.trades[0].direction == -1
    assert result.trades[0].pnl > 0


def test_open_position_force_closed_at_end_of_backtest():
    df = base_columns(n=6, trigger_bar=1)  # no stop/target reachable -> stays open
    strategy = make_strategy("no_exit_probe", "buy_trigger", stop_loss=None, take_profit=None)
    engine = make_engine([strategy])

    result = engine.run(df)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end_of_backtest"


# --- fees really reduce recorded pnl -----------------------------------------


def test_fees_reduce_trade_pnl():
    df = base_columns(n=6, trigger_bar=1)
    strategy = make_strategy("fee_probe", "buy_trigger", stop_loss=None, take_profit=None)

    engine_no_fees = make_engine([strategy], fee_bps=0.0)
    engine_with_fees = make_engine(
        [make_strategy("fee_probe", "buy_trigger", stop_loss=None, take_profit=None)], fee_bps=50.0
    )  # 0.5% each way

    pnl_no_fees = engine_no_fees.run(df).trades[0].pnl
    pnl_with_fees = engine_with_fees.run(df).trades[0].pnl

    assert pnl_with_fees < pnl_no_fees


# --- multi-strategy: independent, concurrent positions ----------------------


def test_two_strategies_hold_independent_concurrent_positions():
    df = base_columns(n=8, trigger_col="buy_trigger_a")
    df["buy_trigger_b"] = 0
    df.loc[1, "buy_trigger_a"] = 1
    df.loc[2, "buy_trigger_b"] = 1

    strategy_a = make_strategy("strat_a", "buy_trigger_a", stop_loss=None, take_profit=None)
    strategy_b = make_strategy("strat_b", "buy_trigger_b", stop_loss=None, take_profit=None)
    engine = make_engine([strategy_a, strategy_b])

    result = engine.run(df)

    strategy_ids = {t.strategy_id for t in result.trades}
    assert strategy_ids == {"strat_a@1.0.0", "strat_b@1.0.0"}
    assert len(result.trades) == 2
