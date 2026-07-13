"""
Builds the default FeatureRegistry with the indicators strategies
currently use. Adding a new feature means adding one registration
here — nothing else in the codebase needs to change.
"""

from datetime import datetime, timezone
from functools import partial

from core.feature_store import FeatureDefinition, FeatureRegistry
from core.indicators import derived, pandas_ta_adapter as ta_adapter


def build_default_registry() -> FeatureRegistry:
    registry = FeatureRegistry()
    now = datetime.now(timezone.utc).isoformat()

    registry.register(FeatureDefinition(
        name="ema_20", version="v1",
        formula=partial(ta_adapter.compute_ema, period=20),
        parameters={"period": 20}, dependencies=[], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="ema_50", version="v1",
        formula=partial(ta_adapter.compute_ema, period=50),
        parameters={"period": 50}, dependencies=[], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="rsi_14", version="v1",
        formula=partial(ta_adapter.compute_rsi, period=14),
        parameters={"period": 14}, dependencies=[], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="atr_14", version="v1",
        formula=partial(ta_adapter.compute_atr, period=14),
        parameters={"period": 14}, dependencies=[], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="adx_14", version="v1",
        formula=partial(ta_adapter.compute_adx, period=14),
        parameters={"period": 14}, dependencies=[], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="atr_percentile_90", version="v1",
        formula=partial(derived.compute_atr_percentile, window=90),
        parameters={"window": 90}, dependencies=["atr_14"], last_updated=now,
    ))
    registry.register(FeatureDefinition(
        name="macd_line", version="v1",
        formula=partial(ta_adapter.compute_macd_line, fast=12, slow=26, signal=9),
        parameters={"fast": 12, "slow": 26, "signal": 9}, dependencies=[], last_updated=now,
    ))
    return registry
