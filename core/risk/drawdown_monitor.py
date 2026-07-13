"""
DrawdownMonitor: tiered response computed from PortfolioView's running
peak equity — same math as core/metrics.py's _max_drawdown (negative
fraction of the peak, e.g. -0.23 = 23% drawdown), but incremental/live
instead of after-the-fact over a whole equity curve.

Tiers are magnitude thresholds against the current drawdown:
  tier 0 (normal):     multiplier 1.0
  tier 1 (throttle):    |drawdown| >= tier_1_pct -> multiplier tier_1_factor
  tier 2 (hard stop):   |drawdown| >= tier_2_pct -> multiplier 0.0, no new entries
  tier 3 (kill switch): |drawdown| >= tier_3_pct -> multiplier 0.0; RiskEngine
                         (not this class) is responsible for calling
                         KillSwitch.engage() when it sees tier 3 — DrawdownMonitor
                         only reports the tier, it never engages the kill switch
                         itself (keeps "detect" and "act" separate, same
                         reasoning as CircuitBreaker not writing its own audit log).
"""

from dataclasses import dataclass

from core.portfolio import PortfolioView


@dataclass
class DrawdownTierResult:
    tier: int  # 0 = normal, 1 = throttle, 2 = hard stop, 3 = kill-switch-triggering
    current_drawdown_pct: float  # negative fraction, e.g. -0.12 = 12% drawdown
    size_multiplier: float


class DrawdownMonitor:
    def __init__(
        self, tier_1_pct: float, tier_1_factor: float, tier_2_pct: float, tier_3_pct: float
    ):
        self.tier_1_pct = tier_1_pct
        self.tier_1_factor = tier_1_factor
        self.tier_2_pct = tier_2_pct
        self.tier_3_pct = tier_3_pct

    def evaluate(self, portfolio_view: PortfolioView) -> DrawdownTierResult:
        if portfolio_view.peak_equity <= 0:
            # Degenerate state (peak equity should always be >= the
            # account's initial capital > 0 in practice) — report no
            # drawdown rather than divide by zero.
            return DrawdownTierResult(tier=0, current_drawdown_pct=0.0, size_multiplier=1.0)

        current_drawdown_pct = (
            portfolio_view.equity - portfolio_view.peak_equity
        ) / portfolio_view.peak_equity
        magnitude = -current_drawdown_pct  # drawdown_pct is <= 0; magnitude is >= 0

        if magnitude >= self.tier_3_pct:
            tier, multiplier = 3, 0.0
        elif magnitude >= self.tier_2_pct:
            tier, multiplier = 2, 0.0
        elif magnitude >= self.tier_1_pct:
            tier, multiplier = 1, self.tier_1_factor
        else:
            tier, multiplier = 0, 1.0

        return DrawdownTierResult(
            tier=tier, current_drawdown_pct=current_drawdown_pct, size_multiplier=multiplier
        )
