"""
Wraps pandas-ta so the rest of the application never imports it
directly. If pandas-ta is ever replaced with TA-Lib or hand-written
formulas, only this file (and its registration in register.py) change
— every strategy, the backtest engine, and every FeatureRegistry
consumer stay untouched.

Each function here matches the FeatureDefinition.formula contract:
takes the full DataFrame, returns a Series aligned to its index.
"""

import pandas as pd
import pandas_ta as ta


def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential moving average of close price."""
    return ta.ema(df["close"], length=period)


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index.

    Note: pandas-ta's RSI uses Wilder's smoothing by default — the
    standard convention (matches TradingView and most platforms). If
    RSI is ever hand-rolled to remove the pandas-ta dependency, match
    this smoothing method exactly, or every strategy tuned against
    historical RSI values will silently see different numbers.
    """
    return ta.rsi(df["close"], length=period)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    return ta.atr(df["high"], df["low"], df["close"], length=period)


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — trend STRENGTH, independent of
    direction. Used by the regime detector to filter out weak/choppy
    EMA crossovers that aren't a real trend."""
    result = ta.adx(df["high"], df["low"], df["close"], length=period)
    return result[f"ADX_{period}"]


def compute_macd_line(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.Series:
    """MACD line only (fast EMA - slow EMA). Signal/histogram are
    separate features so strategies can depend on just the piece they need."""
    result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    return result[f"MACD_{fast}_{slow}_{signal}"]
