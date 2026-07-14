"""
Pydantic schemas for the Dashboard overview API (spec section 8) — a
single aggregating response combining several already-built read
paths for one page-load, not a duplicate of any of them (the
Portfolio/Risk/AI Assistant pages still call their own dedicated
endpoints for deeper views). Two fields are honest "gap" stubs
matching the established available/reason pattern from
PositionsResponseOut/EquityCurveResponseOut — see
api/routes/dashboard.py's module docstring for why.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.portfolio import EquityCurveResponseOut
from api.schemas.risk import RiskDecisionRecordOut


class UnavailableOut(BaseModel):
    available: bool = False
    reason: str


class LatestDailySummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    explanation_id: int
    subject_id: str
    generated_text: str
    generated_at: datetime


class DashboardOverviewOut(BaseModel):
    mode: UnavailableOut
    open_position_count: UnavailableOut
    today_pnl: UnavailableOut
    equity_curve: EquityCurveResponseOut
    recent_risk_decisions: list[RiskDecisionRecordOut] = Field(default_factory=list)
    latest_daily_summary: LatestDailySummaryOut | None = None
