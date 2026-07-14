"""
Pydantic schemas mirroring core.risk's dataclasses field-for-field
(spec section 4). Control-surface schemas (engage/disengage requests)
are deliberately NOT here yet — this is the read-only Risk monitoring
page (spec section 14); control endpoints are a later, dedicated step
(spec decision #4).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RiskConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_config_id: str
    version: str
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    drawdown_tier_1_pct: float
    drawdown_tier_1_factor: float
    drawdown_tier_2_pct: float
    drawdown_tier_3_pct: float
    max_gross_exposure_pct: float
    max_net_exposure_pct: float
    max_concurrent_positions: int
    max_same_symbol_directional_exposure_pct: float
    sizing_method: str
    kelly_fraction_multiplier: float
    kelly_min_sample_size: int
    circuit_breaker_atr_percentile_threshold: float
    circuit_breaker_confirmation_bars: int
    kill_switch_auto_flatten: bool


class KillSwitchStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scope: str
    engaged: bool
    engaged_at: datetime | None
    engaged_reason: str | None
    engaged_by: str | None
    updated_at: datetime | None


class CircuitBreakerStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    breaker_name: str
    tripped: bool
    reason: str | None
    occurred_at: datetime


class LayerResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    layer_name: str
    passed: bool
    multiplier: float
    reason: str | None


class KillSwitchEngageIn(BaseModel):
    reason: str = Field(min_length=1)


class ArmRequestIn(BaseModel):
    strategy_id: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    mainnet: bool = False


class DisarmRequestIn(BaseModel):
    strategy_id: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ArmingStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    strategy_id: str
    exchange: str
    armed: bool
    armed_at: datetime | None
    expires_at: datetime | None
    armed_by: str | None
    mainnet: bool


class RiskDecisionRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    experiment_id: int | None
    bar_time: datetime
    strategy_id: str
    proposed_quantity: float
    approved_quantity: float
    rejection_reason: str | None
    throttle_reasons: list[str]
    layer_results: list[LayerResultOut]
    risk_config_id: str | None
