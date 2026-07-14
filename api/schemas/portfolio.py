"""
Pydantic schemas mirroring core.execution.account_reader's dataclasses
field-for-field (spec section 4).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    starting_balance: float
    current_cash: float
    created_at: datetime


class EquitySnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    equity: float
    open_position_count: int
    snapshot_at: datetime


class EquityCurveResponseOut(BaseModel):
    """`available=False` when no account_snapshots rows exist yet for
    this account — see core/execution/account_reader.py's module
    docstring. An empty equity curve and "nothing has ever been
    recorded" are different facts and must render differently, same
    reasoning as PositionsResponseOut."""

    available: bool
    reason: str | None
    snapshots: list[EquitySnapshotOut] = Field(default_factory=list)
