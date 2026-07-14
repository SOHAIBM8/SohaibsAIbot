"""
Positions API (spec section 12/26) — deliberately scoped down. There
is no `positions` table anywhere in schema.sql, and `Portfolio`'s open-
position tracking (core/portfolio.py) is purely in-memory per backtest/
live-run process — nothing persists it. This is a real, load-bearing
gap, not an oversight: see core/execution/order_manager.py's own
module docstring ("no position tracking yet — Stage 1's schema has no
positions table") and CLAUDE.md's account_snapshots gap, which is the
same shape.

Deriving an approximate "position" (net filled quantity per symbol/
strategy, weighted average entry price) from orders/fills here would
be a second, divergent implementation of position tracking living
outside Portfolio — exactly what CLAUDE.md forbids ("never a second
implementation of trading logic") and what this dashboard build's own
instructions forbid ("Keep all deterministic trading logic inside the
existing backend"). So this endpoint returns a structured
"unavailable" response instead of fabricated numbers — the frontend
(built later) can render an explicit "not yet available" state rather
than a table that looks identical to "you have zero open positions,"
which would be a different and false claim.
"""

from fastapi import APIRouter, Depends

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.schemas.orders import PositionsResponseOut

router = APIRouter(prefix="/api/positions", tags=["positions"])

_UNAVAILABLE_REASON = (
    "No open-position data source exists yet: there is no `positions` table in the schema, "
    "and Portfolio's position tracking is in-memory only, per backtest/live-run process. "
    "See CLAUDE.md's known-limitations section."
)


@router.get("", response_model=PositionsResponseOut)
def list_positions(
    _session: DashboardSession = Depends(get_current_session),
) -> PositionsResponseOut:
    return PositionsResponseOut(available=False, reason=_UNAVAILABLE_REASON, positions=[])
