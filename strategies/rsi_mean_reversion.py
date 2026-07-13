from datetime import datetime, timezone
from core.strategy_base import StrategyBase, StrategyMeta, Signal, Regime


class RSIMeanReversionStrategy(StrategyBase):
    """Long when RSI is oversold, short when overbought. Explicitly
    declared as sideways-only — this strategy fights trends and should
    never be eligible in a trending regime."""

    meta = StrategyMeta(
        name="rsi_mean_reversion",
        version="1.0.0",
        author="platform",
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        description="RSI-14 oversold/overbought mean reversion.",
        parameters={"period": 14, "oversold": 30, "overbought": 70},
        compatible_pipeline_versions=["features_v1"],
        works_best_in=[Regime.SIDEWAYS],
    )
    required_features = ["close", "rsi_14"]
    min_lookback = 14

    def generate_signal(self, feature_window) -> Signal:
        rsi = feature_window.get("rsi_14")
        close = feature_window.get("close")
        oversold, overbought = self.meta.parameters["oversold"], self.meta.parameters["overbought"]

        if rsi <= oversold:
            strength = min((oversold - rsi) / oversold, 1.0)
            return Signal(
                direction=1, entry_price=close, stop_loss=None, take_profit=None,
                strategy_id=self.meta.strategy_id, signal_strength=strength,
                reasons=[f"rsi={rsi:.1f} <= oversold threshold {oversold}"],
            )
        if rsi >= overbought:
            strength = min((rsi - overbought) / (100 - overbought), 1.0)
            return Signal(
                direction=-1, entry_price=close, stop_loss=None, take_profit=None,
                strategy_id=self.meta.strategy_id, signal_strength=strength,
                reasons=[f"rsi={rsi:.1f} >= overbought threshold {overbought}"],
            )
        return Signal(
            direction=0, entry_price=close, stop_loss=None, take_profit=None,
            strategy_id=self.meta.strategy_id, signal_strength=0.0,
            rejected_reasons=[f"rsi={rsi:.1f} within neutral band ({oversold}-{overbought})"],
        )
