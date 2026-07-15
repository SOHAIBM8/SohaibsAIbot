"""
Feature registry: the formal catalog of every computable feature, its
formula, parameters, and dependencies. Strategies declare WHAT they
need (["ema_20", "ema_50", "rsi_14"]); the engine resolves dependency
order and computes only those, using the registered version of each
formula. Nothing hand-stores an indicator value that isn't traceable
back to a versioned function.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass
class FeatureDefinition:
    """
    formula contract: Callable[[pd.DataFrame], pd.Series]

    Operates on the FULL historical DataFrame at once (vectorized), not
    bar-by-bar. This is a deliberate change from the original per-bar
    sketch: indicators like EMA/RSI are recursive (each value depends
    on the prior one), so computing them one bar at a time would either
    require hidden state in the formula — which breaks the determinism
    strategies rely on — or recompute the entire history on every bar,
    which is correct but O(n^2) over a backtest. Vectorized computation
    over the full series, once, avoids both problems.
    """

    name: str
    version: str
    formula: Callable[[pd.DataFrame], pd.Series]
    parameters: dict
    dependencies: list[str]  # other feature names this one needs first
    last_updated: str


class FeatureRegistry:
    def __init__(self) -> None:
        self._defs: dict[str, FeatureDefinition] = {}

    def register(self, definition: FeatureDefinition) -> None:
        self._defs[definition.name] = definition

    def has(self, name: str) -> bool:
        return name in self._defs

    def resolve_order(self, requested: list[str]) -> list[str]:
        """Topological sort over dependencies, e.g. 'macd' computes only
        after 'ema_12' and 'ema_26' if it depends on them. Raises on
        circular dependencies rather than silently looping."""
        visited: set[str] = set()
        order: list[str] = []

        def visit(name: str, stack: list[str]) -> None:
            if name in visited:
                return
            if name in stack:
                raise ValueError(f"circular feature dependency: {' -> '.join(stack)} -> {name}")
            defn = self._defs[name]
            for dep in defn.dependencies:
                visit(dep, stack + [name])
            visited.add(name)
            order.append(name)

        for name in requested:
            visit(name, [])
        return order

    def compute(self, df: pd.DataFrame, requested: list[str]) -> pd.DataFrame:
        """Compute requested features and their dependencies, in the
        correct order, over the full DataFrame. Returns a new DataFrame
        with one column added per feature — never mutates the input."""
        order = self.resolve_order(requested)
        result = df.copy()
        for name in order:
            defn = self._defs[name]
            result[name] = defn.formula(result)
        return result


class FeatureWindow:
    """What a strategy actually receives: computed feature values as of
    a specific bar, and nothing beyond it. Enforcing that boundary here,
    once, centrally, is what prevents every individual strategy from
    having to reimplement its own lookahead-bias protection."""

    def __init__(self, symbol: str, timeframe: str, as_of: datetime, values: dict[str, Any]):
        self.symbol = symbol
        self.timeframe = timeframe
        self.as_of = as_of
        self._values = values

    def get(self, feature_name: str) -> Any:
        if feature_name not in self._values:
            raise KeyError(
                f"'{feature_name}' not available as of {self.as_of} — "
                f"check it isn't accidentally a future-looking feature"
            )
        return self._values[feature_name]
