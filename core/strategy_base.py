from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Regime(Enum):
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    SIDEWAYS = "sideways"


class VolRegime(Enum):
    HIGH_VOL = "high_vol"
    NORMAL_VOL = "normal_vol"
    LOW_VOL = "low_vol"


@dataclass
class Signal:
    """A strategy's raw output. No confidence field here on purpose —
    confidence is computed downstream by ConfidenceEngine, which has
    access to historical performance, regime context, and multi-timeframe
    confirmation that an individual strategy shouldn't need to know about.

    entry_price / stop_loss / take_profit are PROPOSALS from the strategy.
    The risk engine (Phase 3) has final authority to resize or override
    them based on account-level risk limits before anything reaches an
    exchange.
    """
    direction: int                  # 1 long, -1 short, 0 flat
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    strategy_id: str                # "ema_cross@1.0.0"
    signal_strength: float          # 0-1, strategy's own deterministic
                                     # conviction (e.g. crossover gap size) —
                                     # NOT a probability estimate
    reasons: list[str] = field(default_factory=list)            # why generated
    rejected_reasons: list[str] = field(default_factory=list)   # near-miss log
    metadata: dict = field(default_factory=dict)
    generated_at: Optional[datetime] = None


@dataclass
class StrategyMeta:
    name: str
    version: str
    author: str
    created_at: datetime
    description: str
    parameters: dict
    compatible_pipeline_versions: list[str]
    works_best_in: list[Regime]
    works_best_in_vol: list[VolRegime] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return f"{self.name}@{self.version}"


class StrategyBase(ABC):
    meta: StrategyMeta
    required_features: list[str]
    min_lookback: int

    @abstractmethod
    def generate_signal(self, feature_window: "FeatureWindow") -> Signal:
        """Pure function: same input always produces the same output.
        No I/O, no wall-clock reads, no hidden state. This purity is what
        makes backtest and live execution call the same code path, which
        is what makes backtest results trustworthy in the first place."""
        ...

    def validate(self, feature_registry: "FeatureRegistry") -> list[str]:
        errors = []
        for f in self.required_features:
            if not feature_registry.has(f):
                errors.append(f"{self.meta.strategy_id}: unknown feature '{f}'")
        return errors
