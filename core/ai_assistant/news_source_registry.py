"""
Structural copy of StrategyRegistry's discovery pattern (decision #4):
discover() imports every module under a package, finds
NewsSourceAdapter subclasses, and registers them — a broken adapter's
constructor failing is captured and surfaced via rejected(), not
allowed to crash the whole discovery pass or silently vanish.
"""

import importlib
import inspect
import pkgutil

from core.ai_assistant.news_source_adapter import NewsSourceAdapter


class NewsSourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, NewsSourceAdapter] = {}
        self._rejected: dict[str, str] = {}

    def discover(self, package: str = "news_sources") -> None:
        pkg = importlib.import_module(package)
        for _, module_name, _ in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            module = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, NewsSourceAdapter) and obj is not NewsSourceAdapter:
                    try:
                        self.register(obj())
                    except Exception as exc:
                        self._rejected[obj.__name__] = str(exc)

    def register(self, adapter: NewsSourceAdapter) -> None:
        """Direct registration — used by discover() and by callers/tests
        that need to supply constructor args discover() can't."""
        self._adapters[adapter.source_name] = adapter

    def get_all(self) -> list[NewsSourceAdapter]:
        return list(self._adapters.values())

    def rejected(self) -> dict[str, str]:
        """Surface discovery failures so a broken adapter doesn't just
        silently vanish from a sweep."""
        return self._rejected
