"""
Read-only access to paper_accounts/account_snapshots — added for the
dashboard's Portfolio page (docs/dashboard_ui_spec.md section 10).

account_snapshots is a real table with a real schema, but nothing in
this codebase writes to it yet (see core/ai_assistant/context_builder.py's
module docstring note 3, and core/execution/order_manager.py's own
docstring — Stage 1 tracks cash only, no snapshot-writing logic).
list_equity_curve() queries it honestly: if a real writer starts
populating it later, this reader already works correctly with zero
changes; until then it correctly returns an empty list, which the API
layer surfaces as an explicit "no snapshots recorded yet" rather than
silently rendering an empty chart that looks like "zero equity."
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class AccountRecord:
    account_id: str
    starting_balance: float
    current_cash: float
    created_at: datetime


@dataclass
class EquitySnapshotRecord:
    id: int
    account_id: str
    equity: float
    open_position_count: int
    snapshot_at: datetime


class AccountReader:
    def __init__(self, db: Session):
        self.db = db

    def get_account(self, account_id: str) -> AccountRecord | None:
        row = (
            self.db.execute(
                text("""
                    SELECT account_id, starting_balance, current_cash, created_at
                    FROM paper_accounts
                    WHERE account_id = :account_id
                    """),
                {"account_id": account_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return AccountRecord(
            account_id=row["account_id"],
            starting_balance=float(row["starting_balance"]),
            current_cash=float(row["current_cash"]),
            created_at=row["created_at"],
        )

    def list_equity_curve(
        self, account_id: str, limit: int = 500, offset: int = 0
    ) -> list[EquitySnapshotRecord]:
        rows = (
            self.db.execute(
                text("""
                    SELECT id, account_id, equity, open_position_count, snapshot_at
                    FROM account_snapshots
                    WHERE account_id = :account_id
                    ORDER BY snapshot_at ASC
                    LIMIT :limit OFFSET :offset
                    """),
                {"account_id": account_id, "limit": limit, "offset": offset},
            )
            .mappings()
            .all()
        )
        return [
            EquitySnapshotRecord(
                id=row["id"],
                account_id=row["account_id"],
                equity=float(row["equity"]),
                open_position_count=row["open_position_count"],
                snapshot_at=row["snapshot_at"],
            )
            for row in rows
        ]
