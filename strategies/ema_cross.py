from datetime import UTC, datetime

from core.feature_store import FeatureWindow
from core.strategy_base import Regime, Signal, StrategyBase, StrategyMeta


class EMACrossStrategy(StrategyBase):
    """Reference implementation — copy this file's structure for new
    strategies. Long when EMA20 crosses above EMA50, short on the
    inverse cross. Flat otherwise."""

    meta = StrategyMeta(
        name="ema_cross",
        version="1.0.0",
        author="platform",
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
        description="EMA20/EMA50 crossover trend follower.",
        parameters={"fast_period": 20, "slow_period": 50},
        compatible_pipeline_versions=["features_v1"],
        works_best_in=[Regime.BULL_TREND, Regime.BEAR_TREND],
    )
    required_features = ["close", "ema_20", "ema_50", "ema_20_prev", "ema_50_prev"]
    min_lookback = 50

    def generate_signal(self, feature_window: FeatureWindow) -> Signal:
        fast = feature_window.get("ema_20")
        slow = feature_window.get("ema_50")
        fast_prev = feature_window.get("ema_20_prev")
        slow_prev = feature_window.get("ema_50_prev")
        close = feature_window.get("close")

        crossed_up = fast_prev <= slow_prev and fast > slow
        crossed_down = fast_prev >= slow_prev and fast < slow
        gap_pct = abs(fast - slow) / slow if slow else 0.0

        if crossed_up:
            return Signal(
                direction=1,
                entry_price=close,
                stop_loss=None,
                take_profit=None,
                strategy_id=self.meta.strategy_id,
                signal_strength=min(gap_pct * 20, 1.0),
                reasons=[f"ema20 crossed above ema50 (gap={gap_pct:.4f})"],
            )
        if crossed_down:
            return Signal(
                direction=-1,
                entry_price=close,
                stop_loss=None,
                take_profit=None,
                strategy_id=self.meta.strategy_id,
                signal_strength=min(gap_pct * 20, 1.0),
                reasons=[f"ema20 crossed below ema50 (gap={gap_pct:.4f})"],
            )
        return Signal(
            direction=0,
            entry_price=close,
            stop_loss=None,
            take_profit=None,
            strategy_id=self.meta.strategy_id,
            signal_strength=0.0,
            rejected_reasons=["no crossover on this bar"],
        )
