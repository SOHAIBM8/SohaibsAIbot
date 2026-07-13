import pandas as pd
import pytest

from core.feature_store import FeatureDefinition, FeatureRegistry


def make_registry_with_chain() -> FeatureRegistry:
    """a depends on nothing, b depends on a, c depends on b — used to
    verify resolve_order and compute() respect dependency ordering."""
    registry = FeatureRegistry()
    registry.register(FeatureDefinition(
        name="a", version="v1",
        formula=lambda df: df["close"] * 2,
        parameters={}, dependencies=[], last_updated="2026-01-01",
    ))
    registry.register(FeatureDefinition(
        name="b", version="v1",
        formula=lambda df: df["a"] + 1,
        parameters={}, dependencies=["a"], last_updated="2026-01-01",
    ))
    registry.register(FeatureDefinition(
        name="c", version="v1",
        formula=lambda df: df["b"] * 10,
        parameters={}, dependencies=["b"], last_updated="2026-01-01",
    ))
    return registry


def test_resolve_order_respects_dependencies():
    registry = make_registry_with_chain()
    order = registry.resolve_order(["c"])
    assert order.index("a") < order.index("b") < order.index("c")


def test_resolve_order_detects_circular_dependency():
    registry = FeatureRegistry()
    registry.register(FeatureDefinition(
        name="x", version="v1", formula=lambda df: df["close"],
        parameters={}, dependencies=["y"], last_updated="2026-01-01",
    ))
    registry.register(FeatureDefinition(
        name="y", version="v1", formula=lambda df: df["close"],
        parameters={}, dependencies=["x"], last_updated="2026-01-01",
    ))
    with pytest.raises(ValueError, match="circular"):
        registry.resolve_order(["x"])


def test_compute_produces_correct_values_in_order():
    registry = make_registry_with_chain()
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})

    result = registry.compute(df, ["c"])

    assert list(result["a"]) == [2.0, 4.0, 6.0]
    assert list(result["b"]) == [3.0, 5.0, 7.0]
    assert list(result["c"]) == [30.0, 50.0, 70.0]


def test_compute_does_not_mutate_input_dataframe():
    registry = make_registry_with_chain()
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    original_columns = list(df.columns)

    registry.compute(df, ["c"])

    assert list(df.columns) == original_columns
