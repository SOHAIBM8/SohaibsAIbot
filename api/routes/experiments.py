"""
Experiments API (spec section 15/26) — a thin, authenticated wrapper
over core.experiment.ExperimentTracker. Entirely read-only: browsing
and side-by-side comparison carry zero control-surface risk, which is
exactly why the spec calls this out as the natural first page to
build (section 15).

Experiments are not account-scoped (schema.sql's `experiments` table
has no account_id — a backtest run isn't tied to a paper/live
account), so every authenticated operator sees the same experiment
list. Consistent with this being a single-operator V1 (spec decision
#7).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.experiments import ComparisonTableOut, ExperimentResultOut
from core.experiment import ExperimentTracker

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


@router.get("", response_model=list[ExperimentResultOut])
def list_experiments(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> list[ExperimentResultOut]:
    tracker = ExperimentTracker(db)
    results = tracker.list_experiments(limit=limit, offset=offset)
    return [ExperimentResultOut.model_validate(r) for r in results]


@router.get("/compare", response_model=ComparisonTableOut)
def compare_experiments(
    experiment_ids: list[int] = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> ComparisonTableOut:
    tracker = ExperimentTracker(db)
    table = tracker.compare(experiment_ids)
    return ComparisonTableOut(
        results=[ExperimentResultOut.model_validate(r) for r in table.results]
    )


@router.get("/{experiment_id}", response_model=ExperimentResultOut)
def get_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> ExperimentResultOut:
    tracker = ExperimentTracker(db)
    table = tracker.compare([experiment_id])
    if not table.results:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="experiment not found")
    return ExperimentResultOut.model_validate(table.results[0])
