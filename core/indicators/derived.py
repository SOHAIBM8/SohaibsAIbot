"""
Hand-written derived features — kept out of pandas_ta_adapter.py so
that file stays a pure library wrapper, and this one stays free of
the pandas-ta dependency entirely (it's ordinary pandas).
"""

import pandas as pd


def compute_atr_percentile(df: pd.DataFrame, window: int = 90) -> pd.Series:
    """
    Rank current ATR against its own trailing distribution instead of
    using a raw ATR value. ATR is denominated in price units and isn't
    comparable across time (an ATR of $500 means something very
    different at a $20k BTC price than a $70k one) or across symbols —
    a percentile rank against recent history removes that scale
    dependence without needing to hand-tune price-scaled thresholds.

    Uses a TRAILING window ending at the current bar (not centered) —
    at every row this only looks backward, so there is no lookahead.
    `min_periods=window` means the first `window` bars are NaN rather
    than computed against a partial, misleadingly narrow distribution.
    """
    atr = df["atr_14"]
    return atr.rolling(window=window, min_periods=window).apply(
        lambda w: (w <= w.iloc[-1]).mean(), raw=False
    )
