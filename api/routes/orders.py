"""
Orders API (spec section 11/26). Cancel-order (the one control action
on this page, spec section 11) is built here, gated behind the same
account-ownership check GET /{client_order_id} already enforces.

Architectural note (rule 9 — a real gap discovered while building
this, not a hypothetical): core/execution/paper_execution_adapter.py's
PaperExecutionAdapter was designed assuming ONE long-lived process
handles an order's whole submit->cancel lifecycle, sharing an
in-memory `_orders` cache the whole time. This API constructs a fresh
adapter per request, which never called submit_order() itself — so
cancel_order() would always KeyError without first rehydrating the
adapter's cache from the persisted order via the new
PaperExecutionAdapter.load_order() method (added alongside this
route). This does not change any transition/fill logic, only lets the
adapter be reconstructed from its own already-persisted state.

Scope limit: only 'paper' mode orders can be cancelled from here.
Cancelling a real 'live' order would need BinanceExecutionAdapter
constructed with real, decrypted exchange credentials fetched through
CredentialProvider — significant additional scope (credential fetch,
decrypt-audit logging, real exchange calls) that is not being silently
half-built here. Returns 400 for a live order, not a crash or a
misleading success.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.orders import FillOut, OrderDetailOut, OrderOut
from core.execution.latency_simulator import LatencySimulator
from core.execution.order import Order, OrderState, OrderType
from core.execution.order_manager import OrderManager
from core.execution.order_reader import OrderReader
from core.execution.paper_execution_adapter import PaperExecutionAdapter
from core.execution_model import ExecutionModel
from core.ingestion.event_bus import PostgresEventBus

router = APIRouter(prefix="/api/orders", tags=["orders"])


class _UnusedMarketDataSource:
    """Cancel never reads a reference price (only submit_order() does)
    — confirmed before writing this route. A real MarketDataSource is
    never needed here; this stub exists only to satisfy
    PaperExecutionAdapter's constructor and fails loudly if that
    assumption is ever wrong."""

    def get_last_price(self, symbol: str) -> float:
        raise NotImplementedError(
            f"cancel-order never needs a reference price, but one was requested for {symbol}"
        )


@router.get("", response_model=list[OrderOut])
def list_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    strategy_id: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    state: str | None = Query(default=None),
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> list[OrderOut]:
    reader = OrderReader(db)
    orders = reader.list_orders(
        account_id=session.account_id,
        limit=limit,
        offset=offset,
        strategy_id=strategy_id,
        symbol=symbol,
        mode=mode,
        state=state,
    )
    return [OrderOut.model_validate(o) for o in orders]


@router.get("/{client_order_id}", response_model=OrderDetailOut)
def get_order(
    client_order_id: str,
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> OrderDetailOut:
    reader = OrderReader(db)
    order = reader.get_order(client_order_id, account_id=session.account_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")
    fills = reader.list_fills(client_order_id)
    return OrderDetailOut(
        **OrderOut.model_validate(order).model_dump(),
        fills=[FillOut.model_validate(f) for f in fills],
    )


@router.post("/{client_order_id}/cancel", response_model=OrderOut)
def cancel_order(
    client_order_id: str,
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> OrderOut:
    reader = OrderReader(db)
    record = reader.get_order(client_order_id, account_id=session.account_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")
    if record.mode != "paper":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cancelling a live order is not supported by this dashboard build yet — "
                "it needs real exchange credentials wired through CredentialProvider, "
                "out of scope for this step. See CLAUDE.md's known-limitations section."
            ),
        )

    order = Order(
        client_order_id=record.client_order_id,
        strategy_id=record.strategy_id,
        symbol=record.symbol,
        order_type=OrderType(record.order_type),
        direction=record.direction,
        quantity=record.quantity,
        limit_price=record.limit_price,
        stop_price=record.stop_price,
        mode=record.mode,
        state=OrderState(record.state),
        risk_decision_id=record.risk_decision_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        exchange_order_id=record.exchange_order_id,
    )
    adapter = PaperExecutionAdapter(
        execution_model=ExecutionModel(fee_bps=0.0, slippage_bps=0.0),
        latency_simulator=LatencySimulator(base_ms=0.0, jitter_ms=0.0),
        market_data_source=_UnusedMarketDataSource(),
    )
    adapter.load_order(order)
    manager = OrderManager(
        execution_adapter=adapter,
        event_bus=PostgresEventBus(),
        db_session=db,
        mode="paper",
        account_id=session.account_id,
    )
    try:
        cancelled = manager.cancel(client_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return OrderOut(
        client_order_id=cancelled.client_order_id,
        exchange_order_id=cancelled.exchange_order_id,
        account_id=session.account_id,
        strategy_id=cancelled.strategy_id,
        symbol=cancelled.symbol,
        order_type=cancelled.order_type.value,
        direction=cancelled.direction,
        quantity=cancelled.quantity,
        limit_price=cancelled.limit_price,
        stop_price=cancelled.stop_price,
        mode=cancelled.mode,
        state=cancelled.state.value,
        risk_decision_id=cancelled.risk_decision_id,
        created_at=cancelled.created_at,
        updated_at=cancelled.updated_at,
    )
