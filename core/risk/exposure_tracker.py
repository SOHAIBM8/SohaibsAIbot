"""
ExposureTracker — Phase A (spec decision #5): tracks net directional
exposure across strategies on the SAME symbol. Real pairwise
correlation across DIFFERENT symbols (Phase B) waits for multi-symbol
execution to exist.

Design note: PositionView carries no `symbol` field, and that's not an
omission — BacktestEngine/Portfolio are single-symbol in V1 (see
core/portfolio.py's module docstring), so every position in a
PortfolioView already belongs to the one symbol being traded. "Same-
symbol directional exposure" therefore IS all directional exposure
tracked here; Phase B's schema addition (a symbol field, per-symbol
grouping) is deferred until multi-symbol portfolios actually exist,
per rule 8 — no point building that abstraction against data that
can't yet vary.

Exposure basis: entry notional (entry_price * quantity), not
mark-to-market notional. PositionView intentionally exposes only
unrealized_pnl, not a live current price (see its docstring) — re-
deriving a current price from unrealized_pnl to compute a
mark-to-market notional would be exactly the fragile re-derivation
that docstring warns against. Entry notional is what was actually
committed and is a standard, simpler basis for exposure limits.
"""

from dataclasses import dataclass

from core.portfolio import PortfolioView, PositionView
from core.risk.rejection_reason import RejectionReason


@dataclass
class ExposureResult:
    within_limits: bool
    reason: RejectionReason | None
    gross_exposure_pct: float
    net_exposure_pct: float


class ExposureTracker:
    def __init__(
        self,
        max_gross_pct: float,
        max_net_pct: float,
        max_concurrent: int,
        max_same_symbol_directional_pct: float,
    ):
        self.max_gross_pct = max_gross_pct
        self.max_net_pct = max_net_pct
        self.max_concurrent = max_concurrent
        self.max_same_symbol_directional_pct = max_same_symbol_directional_pct

    def evaluate(self, portfolio_view: PortfolioView, proposed_direction: int) -> ExposureResult:
        positions = portfolio_view.open_positions
        equity = portfolio_view.equity

        gross_pct, net_pct = self._exposure_pct(positions, equity)

        if len(positions) >= self.max_concurrent:
            return ExposureResult(False, RejectionReason.MAX_OPEN_POSITIONS, gross_pct, net_pct)

        if gross_pct >= self.max_gross_pct:
            return ExposureResult(
                False, RejectionReason.EXPOSURE_LIMIT_EXCEEDED, gross_pct, net_pct
            )

        if abs(net_pct) >= self.max_net_pct:
            return ExposureResult(
                False, RejectionReason.EXPOSURE_LIMIT_EXCEEDED, gross_pct, net_pct
            )

        same_direction_pct = self._same_direction_pct(positions, equity, proposed_direction)
        if same_direction_pct >= self.max_same_symbol_directional_pct:
            return ExposureResult(False, RejectionReason.CORRELATION_LIMIT, gross_pct, net_pct)

        return ExposureResult(True, None, gross_pct, net_pct)

    @staticmethod
    def _exposure_pct(positions: list[PositionView], equity: float) -> tuple[float, float]:
        if equity <= 0:
            return 0.0, 0.0
        gross_notional = sum(p.entry_price * p.quantity for p in positions)
        net_notional = sum(p.direction * p.entry_price * p.quantity for p in positions)
        return gross_notional / equity, net_notional / equity

    @staticmethod
    def _same_direction_pct(positions: list[PositionView], equity: float, direction: int) -> float:
        if equity <= 0:
            return 0.0
        same_direction_notional = sum(
            p.entry_price * p.quantity for p in positions if p.direction == direction
        )
        return same_direction_notional / equity
