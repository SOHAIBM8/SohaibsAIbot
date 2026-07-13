"""
Configuration for the rule-based regime detector, loaded from YAML.
Kept as its own module so config schema changes never require editing
detection logic, and so the same detector code can run under
different configs (e.g. a different min_confirmation_bars per
timeframe — see the caveat in regime_detector.py about 90 bars
meaning very different things on a 1h vs 1d chart).
"""

from dataclasses import dataclass

import yaml


@dataclass
class RegimeDetectorConfig:
    trend_adx_threshold: float = 20.0
    vol_lookback_bars: int = 90
    vol_high_percentile: float = 0.70
    vol_low_percentile: float = 0.30
    min_confirmation_bars: int = 3

    @classmethod
    def from_yaml(cls, path: str) -> "RegimeDetectorConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        defaults = cls()
        return cls(
            trend_adx_threshold=raw.get("trend", {}).get(
                "adx_threshold", defaults.trend_adx_threshold
            ),
            vol_lookback_bars=raw.get("volatility", {}).get(
                "lookback_bars", defaults.vol_lookback_bars
            ),
            vol_high_percentile=raw.get("volatility", {}).get(
                "high_percentile", defaults.vol_high_percentile
            ),
            vol_low_percentile=raw.get("volatility", {}).get(
                "low_percentile", defaults.vol_low_percentile
            ),
            min_confirmation_bars=raw.get("hysteresis", {}).get(
                "min_confirmation_bars", defaults.min_confirmation_bars
            ),
        )
