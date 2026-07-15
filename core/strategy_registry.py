import importlib
import inspect
import pkgutil

from core.feature_store import FeatureRegistry
from core.regime_detector import RegimeState
from core.strategy_base import StrategyBase


class StrategyRegistry:
    def __init__(self, feature_registry: FeatureRegistry):
        self.feature_registry = feature_registry
        self._strategies: dict[str, StrategyBase] = {}
        self._rejected: dict[str, list[str]] = {}

    def discover(self, package: str = "strategies") -> None:
        """Import every module under strategies/, find StrategyBase
        subclasses, validate each against the feature registry, and
        register only the ones that pass. A broken strategy fails loud
        at discovery time, not silently mid-backtest."""
        pkg = importlib.import_module(package)
        for _, module_name, _ in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            module = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, StrategyBase) and obj is not StrategyBase:
                    instance = obj()
                    errors = instance.validate(self.feature_registry)
                    if errors:
                        self._rejected[instance.meta.strategy_id] = errors
                    else:
                        self._strategies[instance.meta.strategy_id] = instance

    def get_candidates_for_regime(self, state: RegimeState) -> list[StrategyBase]:
        """A strategy is eligible if the current trend matches its
        declared trend affinity, AND (if it declared any volatility
        affinity at all) the current vol regime also matches. Leaving
        works_best_in_vol empty means 'no opinion on volatility' —
        the strategy stays eligible across all vol regimes."""
        candidates = []
        for s in self._strategies.values():
            if state.trend not in s.meta.works_best_in:
                continue
            if s.meta.works_best_in_vol and state.vol not in s.meta.works_best_in_vol:
                continue
            candidates.append(s)
        return candidates

    def all(self) -> list[StrategyBase]:
        return list(self._strategies.values())

    def rejected(self) -> dict[str, list[str]]:
        """Surface discovery failures so a bad plugin doesn't just
        silently vanish from the run."""
        return self._rejected
