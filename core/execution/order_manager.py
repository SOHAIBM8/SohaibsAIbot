"""
OrderManager: the shared state machine driver, identical for paper and
live (spec decision #1) — only self.execution_adapter differs. Accepts
only orders backed by an already-approved SizingDecision (decision #4):
no code path here places an order without Risk Engine approval.

Design notes (rule 9 — gaps in the spec, filled in and flagged here):

1. `handle_fill()` — not `PaperExecutionAdapter.submit_order()` — is
   what transitions an order to PARTIALLY_FILLED/FILLED, per the
   spec's own docstring for handle_fill(). submit() therefore fetches
   any fills the adapter already produced (via get_fills(), synchronous
   for paper in Stage 1) and routes each through handle_fill() itself,
   rather than trusting the adapter to have already updated order.state
   — this is what keeps handle_fill() usable identically for a future
   Stage 2 live adapter, where fills arrive asynchronously and
   `execution_adapter.submit_order()` legitimately returns a
   still-SUBMITTED order with no fills yet available.

2. Constructor gains two params beyond the spec's literal
   `(execution_adapter, event_bus, db_session)` list: `mode: str` and
   `account_id: str`. Order.mode has to come from somewhere, and
   OrderManager — not the caller of submit() — is what should know it
   (one OrderManager is wired to exactly one adapter, hence one mode).
   `account_id` identifies which paper_accounts row handle_fill()
   updates; nothing in the spec's signatures carries one, but
   `paper_accounts` is keyed by it and DoD requires "a paper account
   ... show[s] a correct balance ... afterward." `starting_balance`
   is optional and, if given, upserts the account row at construction
   so a fresh account_id always exists rather than silently updating
   zero rows on the first fill.

3. Only cash balance is updated on fill (`paper_accounts.current_cash`)
   — no position tracking, since Stage 1's schema has no `positions`
   table (spec section 4 note: "no balances/reconciliation/external-
   trade tables in Stage 1"). `account_snapshots` (equity,
   open_position_count) is a table only for now, same as
   `paper_accounts` was in step 3 — nothing in OrderManager's spec'd
   responsibilities writes to it, and inventing a position-tracking
   model to populate it would be scope creep into what a real
   portfolio/position layer for live trading should look like, which
   is explicitly Stage 2/3 territory.

4. `orders.account_id` (added by docs/ai_assistant_spec.md step 5, an
   additive/nullable column) is now written on every insert. Nothing
   in this file's own spec needed it — OrderManager already knew
   self.account_id in-process — but nothing outside this process could
   ever recover which account an order belonged to without it, and the
   AI assistant's account-scoped daily summary needs exactly that.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.execution.events import OrderCancelled, OrderFilled, OrderSubmitted
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import Fill, Order, OrderState, OrderType
from core.ingestion.event_bus import EventBus
from core.risk.risk_decision import SizingDecision


class OrderManager:
    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        event_bus: EventBus,
        db_session: Session,
        mode: str,
        account_id: str,
        starting_balance: float | None = None,
    ):
        self.execution_adapter = execution_adapter
        self.event_bus = event_bus
        self.db = db_session
        self.mode = mode
        self.account_id = account_id
        self._orders: dict[str, Order] = {}

        if starting_balance is not None:
            self._ensure_account(starting_balance)

    def submit(
        self,
        sizing_decision: SizingDecision,
        strategy_id: str,
        symbol: str,
        order_type: OrderType,
        direction: int,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        if sizing_decision.approved_quantity <= 0:
            raise ValueError(
                "cannot submit an order for a SizingDecision with "
                f"approved_quantity={sizing_decision.approved_quantity} <= 0"
            )
        if sizing_decision.risk_decision_id is None:
            raise ValueError(
                "SizingDecision has no risk_decision_id — every order must originate "
                "from a persisted, risk-engine-approved decision"
            )

        now = datetime.now(UTC)
        order = Order(
            client_order_id=str(uuid.uuid4()),
            strategy_id=strategy_id,
            symbol=symbol,
            order_type=order_type,
            direction=direction,
            quantity=sizing_decision.approved_quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            mode=self.mode,
            state=OrderState.PENDING,
            risk_decision_id=sizing_decision.risk_decision_id,
            created_at=now,
            updated_at=now,
        )
        self._orders[order.client_order_id] = order
        self._insert_order(order)

        order = self.execution_adapter.submit_order(order)
        self._orders[order.client_order_id] = order
        self._update_order(order)
        self.event_bus.publish(
            OrderSubmitted(
                client_order_id=order.client_order_id,
                strategy_id=strategy_id,
                symbol=symbol,
                mode=order.mode,
                occurred_at=order.updated_at,
            )
        )

        for fill in self.execution_adapter.get_fills(order.client_order_id):
            self.handle_fill(fill)

        return order

    def handle_fill(self, fill: Fill) -> None:
        order = self._require_order(fill.client_order_id)
        new_state = OrderState.PARTIALLY_FILLED if fill.is_partial else OrderState.FILLED
        order.transition_to(new_state, fill.filled_at)

        self._insert_fill(fill)
        self._update_order(order)
        self.event_bus.publish(
            OrderFilled(
                client_order_id=fill.client_order_id,
                fill_price=fill.fill_price,
                quantity=fill.quantity,
                is_partial=fill.is_partial,
                occurred_at=fill.filled_at,
            )
        )
        self._apply_fill_to_account(order, fill)

    def cancel(self, client_order_id: str) -> Order:
        order = self.execution_adapter.cancel_order(client_order_id)
        self._orders[client_order_id] = order
        self._update_order(order)
        self.event_bus.publish(
            OrderCancelled(client_order_id=client_order_id, occurred_at=order.updated_at)
        )
        return order

    # --- persistence -----------------------------------------------------

    def _require_order(self, client_order_id: str) -> Order:
        if client_order_id not in self._orders:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        return self._orders[client_order_id]

    def _insert_order(self, order: Order) -> None:
        self.db.execute(
            text("""
                INSERT INTO orders (
                    client_order_id, exchange_order_id, strategy_id, symbol, order_type,
                    direction, quantity, limit_price, stop_price, mode, state,
                    risk_decision_id, created_at, updated_at, account_id
                ) VALUES (
                    :client_order_id, :exchange_order_id, :strategy_id, :symbol, :order_type,
                    :direction, :quantity, :limit_price, :stop_price, :mode, :state,
                    :risk_decision_id, :created_at, :updated_at, :account_id
                )
                """),
            {**self._order_params(order), "account_id": self.account_id},
        )
        self.db.commit()

    def _update_order(self, order: Order) -> None:
        self.db.execute(
            text("""
                UPDATE orders
                SET exchange_order_id = :exchange_order_id, state = :state, updated_at = :updated_at
                WHERE client_order_id = :client_order_id
                """),
            {
                "exchange_order_id": order.exchange_order_id,
                "state": order.state.value,
                "updated_at": order.updated_at,
                "client_order_id": order.client_order_id,
            },
        )
        self.db.commit()

    @staticmethod
    def _order_params(order: Order) -> dict:
        return {
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "strategy_id": order.strategy_id,
            "symbol": order.symbol,
            "order_type": order.order_type.value,
            "direction": order.direction,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
            "mode": order.mode,
            "state": order.state.value,
            "risk_decision_id": order.risk_decision_id,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
        }

    def _insert_fill(self, fill: Fill) -> None:
        self.db.execute(
            text("""
                INSERT INTO fills
                    (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
                VALUES (:client_order_id, :fill_price, :quantity, :fee, :is_partial, :filled_at)
                """),
            {
                "client_order_id": fill.client_order_id,
                "fill_price": fill.fill_price,
                "quantity": fill.quantity,
                "fee": fill.fee,
                "is_partial": fill.is_partial,
                "filled_at": fill.filled_at,
            },
        )
        self.db.commit()

    def _ensure_account(self, starting_balance: float) -> None:
        self.db.execute(
            text("""
                INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
                VALUES (:account_id, :starting_balance, :starting_balance, :created_at)
                ON CONFLICT (account_id) DO NOTHING
                """),
            {
                "account_id": self.account_id,
                "starting_balance": starting_balance,
                "created_at": datetime.now(UTC),
            },
        )
        self.db.commit()

    def _apply_fill_to_account(self, order: Order, fill: Fill) -> None:
        # direction: +1 buy (cash out), -1 sell (cash in) — fee always
        # reduces cash. Matches core/portfolio.py's Portfolio cash math.
        notional = fill.fill_price * fill.quantity
        cash_delta = (-notional if order.direction > 0 else notional) - fill.fee
        self.db.execute(
            text("""
                UPDATE paper_accounts
                SET current_cash = current_cash + :cash_delta
                WHERE account_id = :account_id
                """),
            {"cash_delta": cash_delta, "account_id": self.account_id},
        )
        self.db.commit()
