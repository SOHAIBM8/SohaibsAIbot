"""
Portfolio API (spec section 10/26) — read-only. Two of the three data
elements section 10 asks for are already served elsewhere, deliberately
not duplicated here:

- "Exposure figures from ExposureTracker, daily/weekly PnL and limit
  status from LossLimitTracker" — same as Step 4's drawdown tier: these
  are stateless calculators with no persisted "current" state; the one
  real, persisted trace of what they last decided is risk_decision_log,
  already exposed at GET /api/risk/decisions. The Portfolio page (built
  later) reads that same endpoint rather than this module re-querying
  the same table under a different path.
- "Open position summary" — see api/routes/positions.py; no persisted
  data source exists yet.

What IS real and served here: the account's current cash/starting
balance (paper_accounts), and the equity curve (account_snapshots) —
empty today since nothing writes to it yet, surfaced honestly via
`available=False` rather than a chart that looks like zero equity.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.portfolio import AccountOut, EquityCurveResponseOut, EquitySnapshotOut
from core.execution.account_reader import AccountReader

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_NO_SNAPSHOTS_REASON = (
    "No account_snapshots rows exist yet for this account: nothing in the execution layer "
    "currently writes them (a known, documented gap — see CLAUDE.md's known-limitations "
    "section). The equity curve will populate once a snapshot writer exists."
)


@router.get("/account", response_model=AccountOut)
def get_account(
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> AccountOut:
    account = AccountReader(db).get_account(session.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    return AccountOut.model_validate(account)


@router.get("/equity-curve", response_model=EquityCurveResponseOut)
def get_equity_curve(
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> EquityCurveResponseOut:
    snapshots = AccountReader(db).list_equity_curve(session.account_id, limit=limit, offset=offset)
    if not snapshots:
        return EquityCurveResponseOut(available=False, reason=_NO_SNAPSHOTS_REASON, snapshots=[])
    return EquityCurveResponseOut(
        available=True,
        reason=None,
        snapshots=[EquitySnapshotOut.model_validate(s) for s in snapshots],
    )
