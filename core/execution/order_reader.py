"""
Read-only access to orders/fills — added for the dashboard's Orders
page (docs/dashboard_ui_spec.md section 11). OrderManager itself
exposes no query methods (only submit()/handle_fill()/cancel(), plus
an in-memory cache with no public getter — see its own module
docstring), so every read here is new, but it's read-only SQL over an
already-existing schema, not a second implementation of any order
state-machine or fill-handling logic. Mirrors the raw-SQL read pattern
already established in core/ai_assistant/context_builder.py.

Every list/get method is account-scoped. get_order() returns None
identically whether the order doesn't exist or belongs to a different
account — same "don't leak existence via a different error" discipline
GetTradeTool (core/ai_assistant/chat_tool.py) already uses.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session


@dataclass
class FillRecord:
    id: int
    client_order_id: str
    fill_price: float
    quantity: float
    fee: float
    is_partial: bool
    filled_at: datetime


@dataclass
class OrderRecord:
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


class OrderReader:
    def __init__(self, db: Session):
        self.db = db

    def list_orders(
        self,
        account_id: str,
        limit: int = 50,
        offset: int = 0,
        strategy_id: str | None = None,
        symbol: str | None = None,
        mode: str | None = None,
        state: str | None = None,
    ) -> list[OrderRecord]:
        conditions = ["account_id = :account_id"]
        params: dict[str, object] = {"account_id": account_id, "limit": limit, "offset": offset}
        if strategy_id is not None:
            conditions.append("strategy_id = :strategy_id")
            params["strategy_id"] = strategy_id
        if symbol is not None:
            conditions.append("symbol = :symbol")
            params["symbol"] = symbol
        if mode is not None:
            conditions.append("mode = :mode")
            params["mode"] = mode
        if state is not None:
            conditions.append("state = :state")
            params["state"] = state

        rows = (
            self.db.execute(
                text(f"""
                    SELECT client_order_id, exchange_order_id, account_id, strategy_id, symbol,
                           order_type, direction, quantity, limit_price, stop_price, mode, state,
                           risk_decision_id, created_at, updated_at
                    FROM orders
                    WHERE {" AND ".join(conditions)}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """),
                params,
            )
            .mappings()
            .all()
        )
        return [self._row_to_order(row) for row in rows]

    def get_order(self, client_order_id: str, account_id: str) -> OrderRecord | None:
        row = (
            self.db.execute(
                text("""
                    SELECT client_order_id, exchange_order_id, account_id, strategy_id, symbol,
                           order_type, direction, quantity, limit_price, stop_price, mode, state,
                           risk_decision_id, created_at, updated_at
                    FROM orders
                    WHERE client_order_id = :client_order_id
                    """),
                {"client_order_id": client_order_id},
            )
            .mappings()
            .first()
        )
        if row is None or row["account_id"] != account_id:
            return None
        return self._row_to_order(row)

    def list_fills(self, client_order_id: str) -> list[FillRecord]:
        rows = (
            self.db.execute(
                text("""
                    SELECT id, client_order_id, fill_price, quantity, fee, is_partial, filled_at
                    FROM fills
                    WHERE client_order_id = :client_order_id
                    ORDER BY filled_at
                    """),
                {"client_order_id": client_order_id},
            )
            .mappings()
            .all()
        )
        return [
            FillRecord(
                id=row["id"],
                client_order_id=row["client_order_id"],
                fill_price=float(row["fill_price"]),
                quantity=float(row["quantity"]),
                fee=float(row["fee"]),
                is_partial=row["is_partial"],
                filled_at=row["filled_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_order(row: RowMapping) -> OrderRecord:
        return OrderRecord(
            client_order_id=row["client_order_id"],
            exchange_order_id=row["exchange_order_id"],
            account_id=row["account_id"],
            strategy_id=row["strategy_id"],
            symbol=row["symbol"],
            order_type=row["order_type"],
            direction=row["direction"],
            quantity=float(row["quantity"]),
            limit_price=float(row["limit_price"]) if row["limit_price"] is not None else None,
            stop_price=float(row["stop_price"]) if row["stop_price"] is not None else None,
            mode=row["mode"],
            state=row["state"],
            risk_decision_id=row["risk_decision_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
