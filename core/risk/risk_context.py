"""
Everything the Risk Engine needs to make one sizing decision, bundled
so `RiskEngine.size()` has a single, testable input shape instead of a
growing parameter list. `PortfolioView` (not `Portfolio`) is embedded
deliberately — the Risk Engine must never be able to mutate portfolio
state, only read it.
"""

from dataclasses import dataclass
from datetime import datetime

from core.feature_store import FeatureWindow
from core.portfolio import PortfolioView
from core.regime_detector import RegimeState


@dataclass
class RiskContext:
    equity: float
    feature_window: FeatureWindow
    regime_state: RegimeState
    portfolio_view: PortfolioView
    data_quality_ok: bool
    data_quality_reason: str | None
    as_of: datetime
