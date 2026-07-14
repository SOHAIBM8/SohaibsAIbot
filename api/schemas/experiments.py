"""
Pydantic schemas mirroring core.experiment's dataclasses field-for-
field (spec section 4: "trading semantics are never redefined at this
layer"). Nothing here recomputes a metric or reshapes a result —
serialization only.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExperimentConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_ids: list[str]
    symbol: str
    timeframe: str
    date_range: tuple[str, str]
    feature_pipeline_version: str
    fee_bps: float
    slippage_model: str
    code_commit_hash: str
    risk_config_id: str | None


class ExperimentResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    experiment_id: int
    config: ExperimentConfigOut
    started_at: datetime
    finished_at: datetime | None
    metrics: dict
    equity_curve_path: str | None
    notes: str


class ComparisonTableOut(BaseModel):
    results: list[ExperimentResultOut]
