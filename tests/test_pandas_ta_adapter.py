import numpy as np
import pandas as pd
import pytest

from core.indicators import pandas_ta_adapter as ta_adapter
from core.indicators.register import build_default_registry


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    """100 bars of synthetic but realistic-shaped OHLCV data — enough
    history for EMA50/RSI14/ATR14 to produce non-NaN values."""
    rng = np.random.default_rng(seed=42)
    n = 100
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0, 1, n)
    low = close - rng.uniform(0, 1, n)
    return pd.DataFrame({"close": close, "high": high, "low": low})


def test_compute_ema_matches_pandas_ta_directly(sample_ohlcv):
    """The adapter should be a thin pass-through — no logic of its own
    to diverge from pandas-ta's own output."""
    import pandas_ta as ta

    expected = ta.ema(sample_ohlcv["close"], length=20)
    actual = ta_adapter.compute_ema(sample_ohlcv, period=20)

    pd.testing.assert_series_equal(actual, expected)


def test_compute_rsi_bounded_between_0_and_100(sample_ohlcv):
    rsi = ta_adapter.compute_rsi(sample_ohlcv, period=14)
    valid = rsi.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_compute_atr_is_non_negative(sample_ohlcv):
    atr = ta_adapter.compute_atr(sample_ohlcv, period=14)
    assert (atr.dropna() >= 0).all()


def test_default_registry_computes_all_registered_features(sample_ohlcv):
    """End-to-end: register.py's default registry can actually compute
    every feature strategies currently declare as required."""
    registry = build_default_registry()

    result = registry.compute(sample_ohlcv, ["ema_20", "ema_50", "rsi_14", "atr_14", "macd_line"])

    for col in ["ema_20", "ema_50", "rsi_14", "atr_14", "macd_line"]:
        assert col in result.columns
        assert result[col].notna().any(), f"{col} is all-NaN — check lookback/period"


def test_default_registry_computes_every_real_strategys_required_features(sample_ohlcv):
    """Regression guard: EMACrossStrategy.required_features included
    "ema_20_prev"/"ema_50_prev" for a long time without either ever
    being registered — the real strategy would raise KeyError the
    first time it ran against a real FeatureRegistry, and nothing
    caught it because no test ever requested exactly the feature set a
    real strategy actually declares. This test computes each real
    reference strategy's own required_features (minus raw OHLC
    columns, which the registry doesn't compute) directly against the
    default registry, so a future strategy with an unregistered
    required feature fails loudly here instead of silently at runtime."""
    from strategies.ema_cross import EMACrossStrategy
    from strategies.rsi_mean_reversion import RSIMeanReversionStrategy

    registry = build_default_registry()
    ohlc = {"open", "high", "low", "close"}

    for strategy_cls in (EMACrossStrategy, RSIMeanReversionStrategy):
        requested = [f for f in strategy_cls().required_features if f not in ohlc]
        result = registry.compute(sample_ohlcv, requested)
        for col in requested:
            assert col in result.columns
            assert result[col].notna().any(), f"{col} is all-NaN — check lookback/period"
