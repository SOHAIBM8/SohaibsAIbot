"""
Dashboard overview API (spec section 8/26) — one aggregating GET for
the landing page's first paint, combining several already-built read
paths (equity curve, recent risk decisions, latest daily summary).
Unlike Step 6/9's decision not to duplicate a single-purpose endpoint,
aggregation IS this endpoint's entire job — the Portfolio/Risk/AI
Assistant pages still have their own dedicated endpoints for deeper
views; this one exists so the overview page loads in one request
instead of five.

Two fields are honest gap stubs, not fabricated:

- "mode" (spec decision #3: "Current trading mode (paper / testnet /
  mainnet) is a persistent, always-visible UI element"): researched
  before building — there is no single persisted "current mode" value
  anywhere in this schema. `orders.mode` is per-order (paper/live
  only, no testnet/mainnet distinction); `encrypted_credentials.mainnet`/
  `arming_state.mainnet` are per-row booleans scoped to one
  (account, strategy, exchange), not an account-level singleton.
  Guessing from "the most recent order's mode" would be actively
  misleading if concurrent strategies run in different modes. Flagged
  as unavailable rather than guessed.
- "open_position_count": same gap as GET /api/positions (Step 5) — no
  positions table exists. Reused here as a stub, not reimplemented.
- "today_pnl": same gap as Step 4/6's drawdown/exposure findings —
  LossLimitTracker is a stateless calculator needing a live
  PortfolioView this API process cannot construct. recent_risk_decisions
  below is the closest real, persisted signal.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.dashboard import DashboardOverviewOut, LatestDailySummaryOut, UnavailableOut
from api.schemas.portfolio import EquityCurveResponseOut, EquitySnapshotOut
from api.schemas.risk import LayerResultOut, RiskDecisionRecordOut
from core.ai_assistant.explanation_reader import ExplanationReader
from core.execution.account_reader import AccountReader
from core.risk.risk_decision import RiskDecisionLogReader

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_MODE_UNAVAILABLE_REASON = (
    "No single 'current mode' value exists in this schema — mode is tracked per-order "
    "(paper/live) and per-credential/arming-record (mainnet), not as one account-level flag. "
    "See CLAUDE.md's known-limitations section."
)
_POSITION_COUNT_UNAVAILABLE_REASON = (
    "No positions table exists yet — see GET /api/positions and CLAUDE.md's "
    "known-limitations section."
)
_TODAY_PNL_UNAVAILABLE_REASON = (
    "LossLimitTracker needs a live PortfolioView this API process cannot construct — "
    "see recent_risk_decisions for the closest real, persisted signal, and CLAUDE.md's "
    "known-limitations section."
)


@router.get("/overview", response_model=DashboardOverviewOut)
def get_dashboard_overview(
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> DashboardOverviewOut:
    snapshots = AccountReader(db).list_equity_curve(session.account_id, limit=500)
    equity_curve = (
        EquityCurveResponseOut(
            available=True,
            reason=None,
            snapshots=[EquitySnapshotOut.model_validate(s) for s in snapshots],
        )
        if snapshots
        else EquityCurveResponseOut(
            available=False,
            reason="No account_snapshots rows exist yet for this account.",
            snapshots=[],
        )
    )

    decisions = RiskDecisionLogReader(db).list_recent(limit=5)
    recent_risk_decisions = [
        RiskDecisionRecordOut(
            id=r.id,
            experiment_id=r.experiment_id,
            bar_time=r.bar_time,
            strategy_id=r.strategy_id,
            proposed_quantity=r.proposed_quantity,
            approved_quantity=r.approved_quantity,
            rejection_reason=r.rejection_reason.value if r.rejection_reason else None,
            throttle_reasons=[t.value for t in r.throttle_reasons],
            layer_results=[LayerResultOut.model_validate(lr) for lr in r.layer_results],
            risk_config_id=r.risk_config_id,
        )
        for r in decisions
    ]

    summary = ExplanationReader(db).get_latest_daily_summary(session.account_id)
    latest_daily_summary = (
        LatestDailySummaryOut.model_validate(summary) if summary is not None else None
    )

    return DashboardOverviewOut(
        mode=UnavailableOut(reason=_MODE_UNAVAILABLE_REASON),
        open_position_count=UnavailableOut(reason=_POSITION_COUNT_UNAVAILABLE_REASON),
        today_pnl=UnavailableOut(reason=_TODAY_PNL_UNAVAILABLE_REASON),
        equity_curve=equity_curve,
        recent_risk_decisions=recent_risk_decisions,
        latest_daily_summary=latest_daily_summary,
    )
