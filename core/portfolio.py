"""
Tracks cash, open positions (one per strategy at a time in V1), the
trade log, and the equity curve. Positions are keyed by strategy_id so
multiple strategies can hold independent positions concurrently while
sharing one pool of capital — the realistic shape of one account
running several strategies at once.

Single-symbol in V1: mark_to_market takes one current price. Multi-
symbol portfolios are a natural later extension but need position-
sizing/exposure logic that belongs with the Risk Engine, not here.

Short-position cash accounting is simplified (no margin requirement
modeled) — acceptable for V1 backtesting math, but the execution layer
(Phase 2) must model real margin/borrow costs before shorts are traded
live.
"""

from dataclasses import dataclass
from datetime import datetime

from core.execution_model import ExecutionModel


@dataclass
class Position:
    strategy_id: str
    direction: int  # 1 long, -1 short
    entry_price: float  # actual fill price, after slippage
    quantity: float
    entry_time: datetime
    stop_loss: float | None
    take_profit: float | None
    entry_fee: float
    regime_at_entry: str


@dataclass
class Trade:
    strategy_id: str
    direction: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    fees_paid: float
    pnl: float
    pnl_pct: float
    r_multiple: float | None
    exit_reason: str
    regime_at_entry: str


@dataclass
class PositionView:
    """Read-only view of one open position — what the Risk Engine is
    allowed to see. No entry_time/stop_loss/take_profit here on
    purpose: the Risk Engine reasons about exposure and PnL, not about
    re-deriving exit logic that belongs to Portfolio/BacktestEngine."""

    strategy_id: str
    direction: int
    entry_price: float
    quantity: float
    unrealized_pnl: float


@dataclass
class PortfolioView:
    """Read-only snapshot of Portfolio state, produced by
    Portfolio.snapshot(). This — not Portfolio itself — is the Risk
    Engine's only window into portfolio state, per the spec's
    separation of concerns: the Risk Engine reads a PortfolioView, it
    never calls open_position/close_position itself."""

    equity: float
    peak_equity: float
    open_positions: list[PositionView]
    trade_history: list[Trade]  # for LossLimitTracker's UTC-window filtering


class Portfolio:
    def __init__(self, initial_capital: float, execution_model: ExecutionModel):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.execution_model = execution_model
        self.open_positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []

    def open_position(
        self,
        strategy_id: str,
        direction: int,
        reference_price: float,
        quantity: float,
        entry_time: datetime,
        stop_loss: float | None,
        take_profit: float | None,
        regime_at_entry: str,
    ) -> None:
        order_side = direction  # opening long = buy (+1); opening short = sell (-1)
        fill = self.execution_model.fill(reference_price, order_side, quantity)

        if direction > 0:
            self.cash -= fill.fill_price * quantity  # buying: cash leaves
        else:
            self.cash += fill.fill_price * quantity  # short sale proceeds (simplified)
        self.cash -= fill.fee

        self.open_positions[strategy_id] = Position(
            strategy_id=strategy_id,
            direction=direction,
            entry_price=fill.fill_price,
            quantity=quantity,
            entry_time=entry_time,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_fee=fill.fee,
            regime_at_entry=regime_at_entry,
        )

    def close_position(
        self, strategy_id: str, reference_price: float, exit_time: datetime, exit_reason: str
    ) -> None:
        pos = self.open_positions.pop(strategy_id)
        order_side = -pos.direction  # closing a long = sell; closing a short = buy
        fill = self.execution_model.fill(reference_price, order_side, pos.quantity)

        if pos.direction > 0:
            self.cash += fill.fill_price * pos.quantity  # selling long: cash arrives
        else:
            self.cash -= fill.fill_price * pos.quantity  # covering short: cash leaves
        self.cash -= fill.fee

        gross_pnl = (fill.fill_price - pos.entry_price) * pos.quantity * pos.direction
        total_fees = pos.entry_fee + fill.fee
        pnl = gross_pnl - total_fees
        notional = pos.entry_price * pos.quantity
        pnl_pct = pnl / notional if notional else 0.0

        r_multiple = None
        if pos.stop_loss is not None and pos.entry_price != pos.stop_loss:
            risk_per_unit = abs(pos.entry_price - pos.stop_loss)
            r_multiple = ((fill.fill_price - pos.entry_price) * pos.direction) / risk_per_unit

        self.trades.append(
            Trade(
                strategy_id=strategy_id,
                direction=pos.direction,
                entry_time=pos.entry_time,
                exit_time=exit_time,
                entry_price=pos.entry_price,
                exit_price=fill.fill_price,
                quantity=pos.quantity,
                fees_paid=total_fees,
                pnl=pnl,
                pnl_pct=pnl_pct,
                r_multiple=r_multiple,
                exit_reason=exit_reason,
                regime_at_entry=pos.regime_at_entry,
            )
        )

    def mark_to_market(self, timestamp: datetime, current_price: float) -> float:
        equity = self.cash
        for pos in self.open_positions.values():
            if pos.direction > 0:
                equity += current_price * pos.quantity
            else:
                equity -= current_price * pos.quantity
        self.equity_curve.append((timestamp, equity))
        return equity

    def snapshot(self, current_price: float) -> PortfolioView:
        """Read-only. The Risk Engine's only window into Portfolio
        state — mirrors mark_to_market's equity math exactly, but never
        mutates equity_curve or open_positions. peak_equity considers
        both the recorded equity_curve history and this snapshot's own
        (not-yet-recorded) current equity, so a same-bar drawdown check
        sees a peak that's never stale by one bar."""
        equity = self.cash
        position_views = []
        for pos in self.open_positions.values():
            if pos.direction > 0:
                equity += current_price * pos.quantity
                unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
            else:
                equity -= current_price * pos.quantity
                unrealized_pnl = (pos.entry_price - current_price) * pos.quantity
            position_views.append(
                PositionView(
                    strategy_id=pos.strategy_id,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    quantity=pos.quantity,
                    unrealized_pnl=unrealized_pnl,
                )
            )

        historical_peak = max((e for _, e in self.equity_curve), default=self.initial_capital)
        peak_equity = max(historical_peak, equity)

        return PortfolioView(
            equity=equity,
            peak_equity=peak_equity,
            open_positions=position_views,
            trade_history=list(self.trades),
        )
