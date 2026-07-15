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

5. `kill_switch`/`arming_service`/`exchange` (added — CLAUDE.md "What's
   NOT built yet": `is_trading_permitted()`, Stage 3's real KillSwitch
   + ArmingService dual gate, was built and tested in isolation but
   never actually called from the live order-submission path).
   Structural, not best-effort, matching this project's established
   pattern for high-stakes gates (MainnetGate's isinstance() check at
   the lowest layer, kill switch's "never auto-clears"): a `mode="live"`
   OrderManager cannot be constructed at all without both `kill_switch`
   and `arming_service` — there is no code path that produces a live
   OrderManager capable of silently skipping the check because a caller
   forgot to wire it. `mode="paper"` is unaffected — arming/kill-switch
   were never meant to gate simulated trading, and every existing
   paper-mode caller/test is completely unaffected by this change.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.execution.events import OrderCancelled, OrderFilled, OrderSubmitted
from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import Fill, Order, OrderState, OrderType
from core.ingestion.event_bus import EventBus
from core.risk.kill_switch import KillSwitch
from core.risk.risk_decision import SizingDecision
from core.security.arming_service import ArmingService, is_trading_permitted

# Which cash-tracking table a fill's account effect is applied to,
# keyed by Order.mode ('paper' | 'live' — the only two values the
# dataclass allows). Fixed, code-owned whitelist — never derived from
# untrusted input — so this can safely be interpolated into SQL below.
# Added to fix docs/gap_audit_report.md P0 #1: every fill used to be
# written into paper_accounts regardless of mode, mixing simulated and
# real trading results in one ledger.
_ACCOUNT_TABLE_BY_MODE = {"paper": "paper_accounts", "live": "live_accounts"}


def _account_table(mode: str) -> str:
    try:
        return _ACCOUNT_TABLE_BY_MODE[mode]
    except KeyError:
        raise ValueError(
            f"unknown order mode={mode!r} — no account table mapping exists for it"
        ) from None


class TradingNotPermittedError(RuntimeError):
    """Raised by submit() for a live-mode order when the KillSwitch +
    ArmingService dual gate (is_trading_permitted()) refuses — fail
    loud, exactly like submit()'s existing SizingDecision guards,
    never a silent no-op that could look like an order was placed."""


class OrderManager:
    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        event_bus: EventBus,
        db_session: Session,
        mode: str,
        account_id: str,
        starting_balance: float | None = None,
        kill_switch: KillSwitch | None = None,
        arming_service: ArmingService | None = None,
        exchange: str | None = None,
    ):
        if mode == "live" and (kill_switch is None or arming_service is None or not exchange):
            raise ValueError(
                "a mode='live' OrderManager requires kill_switch, arming_service, and "
                "exchange — every live order must pass the KillSwitch + ArmingService "
                "dual gate (docs/execution_engine_stage2_spec.md, CLAUDE.md "
                "'is_trading_permitted()' gap), and there is no supported way to "
                "construct a live OrderManager that skips it"
            )
        self.execution_adapter = execution_adapter
        self.event_bus = event_bus
        self.db = db_session
        self.mode = mode
        self.account_id = account_id
        self.kill_switch = kill_switch
        self.arming_service = arming_service
        self.exchange = exchange
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
        if self.mode == "live":
            self._enforce_trading_permitted(strategy_id)

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

    def _enforce_trading_permitted(self, strategy_id: str) -> None:
        # __init__ guarantees these are non-None whenever self.mode ==
        # "live" — asserted, not re-checked, so this stays the single
        # place that decision is enforced.
        assert self.kill_switch is not None
        assert self.arming_service is not None
        if is_trading_permitted(
            self.kill_switch, self.arming_service, self.account_id, strategy_id, self.exchange or ""
        ):
            return
        # is_trading_permitted() is the single source of truth for the
        # dual-gate decision (never re-derived here) — this cheap
        # follow-up call only builds a specific, honest error message
        # for whichever gate actually refused.
        if self.kill_switch.is_engaged():
            raise TradingNotPermittedError(
                f"kill switch is engaged — live order for strategy_id={strategy_id!r} refused"
            )
        raise TradingNotPermittedError(
            f"strategy_id={strategy_id!r} is not armed for exchange={self.exchange!r} "
            f"(account_id={self.account_id!r}) — live order refused"
        )

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
        table = _account_table(self.mode)
        self.db.execute(
            text(f"""
                INSERT INTO {table} (account_id, starting_balance, current_cash, created_at)
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
        # Routed to paper_accounts or live_accounts by order.mode, not
        # always paper_accounts (see _ACCOUNT_TABLE_BY_MODE's docstring
        # for why this matters — a real fill used to silently corrupt
        # the paper ledger).
        table = _account_table(order.mode)
        notional = fill.fill_price * fill.quantity
        cash_delta = (-notional if order.direction > 0 else notional) - fill.fee
        self.db.execute(
            text(f"""
                UPDATE {table}
                SET current_cash = current_cash + :cash_delta
                WHERE account_id = :account_id
                """),
            {"cash_delta": cash_delta, "account_id": self.account_id},
        )
        self.db.commit()
