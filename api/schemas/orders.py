"""
Pydantic schemas mirroring core.execution.order_reader's dataclasses
field-for-field (spec section 4). Cancel-order (the one control action
on the Orders page, per spec section 11) is deliberately NOT here yet
— control endpoints are a later, dedicated step (spec decision #4).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_order_id: str
    fill_price: float
    quantity: float
    fee: float
    is_partial: bool
    filled_at: datetime


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_order_id: str
    exchange_order_id: str | None
    account_id: str | None
    strategy_id: str
    symbol: str
    order_type: str
    direction: int
    quantity: float
    limit_price: float | None
    stop_price: float | None
    mode: str
    state: str
    risk_decision_id: int
    created_at: datetime
    updated_at: datetime


class OrderDetailOut(OrderOut):
    fills: list[FillOut]


class PositionsResponseOut(BaseModel):
    """No `positions` table exists anywhere in this schema, and
    `Portfolio`'s open-position tracking is purely in-memory per
    backtest/live-run process — there is no persisted source an API
    process can read "current open positions" from (see
    api/routes/positions.py's module docstring). `available=False`
    lets the frontend distinguish "no data source yet" from "flat,
    zero open positions," which are not the same fact and must not be
    displayed identically."""

    available: bool
    reason: str | None
    positions: list[dict] = Field(default_factory=list)
