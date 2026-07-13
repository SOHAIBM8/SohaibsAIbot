"""
Tests run against a real local Postgres (started via `docker compose up -d`,
schema applied from schema.sql) — not mocks. ExperimentTracker's only job is
translating between Python objects and SQL correctly, which a mock can't
verify; these tests catch things like the JSONB round-trip, the DATE-column
unpacking, and RETURNING behavior that a mock would happily let slide.

Every test cleans up the experiment rows it creates so the suite can be
re-run against the same database without accumulating junk.
"""

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.experiment import ExperimentConfig, ExperimentTracker


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def tracker(db):
    return ExperimentTracker(db)


@pytest.fixture
def cleanup(db):
    created_ids = []
    yield created_ids
    if created_ids:
        db.execute(
            text("DELETE FROM experiments WHERE experiment_id = ANY(:ids)"),
            {"ids": created_ids},
        )
        db.commit()


def make_config(**overrides) -> ExperimentConfig:
    defaults = dict(
        strategy_ids=["ema_cross@1.0.0"],
        symbol="BTC/USDT",
        timeframe="1h",
        date_range=("2024-01-01", "2024-06-01"),
        feature_pipeline_version="v1",
        fee_bps=10.0,
        slippage_model="fixed_bps",
        code_commit_hash="deadbeef",
    )
    defaults.update(overrides)
    return ExperimentConfig(**defaults)


def test_start_inserts_row_and_returns_id(tracker, cleanup):
    experiment_id = tracker.start(make_config())
    cleanup.append(experiment_id)

    assert isinstance(experiment_id, int)


def test_finish_updates_metrics_and_can_be_read_back(tracker, db, cleanup):
    experiment_id = tracker.start(make_config())
    cleanup.append(experiment_id)

    metrics = {"sharpe": 1.42, "max_drawdown": -0.18, "win_rate": 0.55}
    tracker.finish(experiment_id, metrics=metrics, equity_curve_path="s3://bucket/eq.parquet")

    table = tracker.compare([experiment_id])
    assert len(table.results) == 1
    result = table.results[0]
    assert result.experiment_id == experiment_id
    assert result.metrics == metrics
    assert result.equity_curve_path == "s3://bucket/eq.parquet"
    assert result.finished_at is not None
    assert result.config.symbol == "BTC/USDT"
    assert result.config.strategy_ids == ["ema_cross@1.0.0"]
    assert result.config.date_range == ("2024-01-01", "2024-06-01")


def test_finish_unknown_experiment_id_raises(tracker):
    with pytest.raises(ValueError):
        tracker.finish(-1, metrics={}, equity_curve_path="x")


def test_compare_keeps_multiple_runs_distinct(tracker, cleanup):
    id_a = tracker.start(make_config(code_commit_hash="commit_a"))
    id_b = tracker.start(make_config(code_commit_hash="commit_b"))
    cleanup.extend([id_a, id_b])

    tracker.finish(id_a, metrics={"sharpe": 1.0}, equity_curve_path="a.parquet")
    tracker.finish(id_b, metrics={"sharpe": 2.0}, equity_curve_path="b.parquet")

    table = tracker.compare([id_a, id_b])

    assert len(table.results) == 2
    by_id = {r.experiment_id: r for r in table.results}
    assert by_id[id_a].config.code_commit_hash == "commit_a"
    assert by_id[id_b].config.code_commit_hash == "commit_b"
    assert table.metric("sharpe") == {id_a: 1.0, id_b: 2.0}


def test_compare_empty_list_returns_empty_table(tracker):
    table = tracker.compare([])
    assert table.results == []


def test_start_before_finish_has_null_metrics(tracker, cleanup):
    experiment_id = tracker.start(make_config())
    cleanup.append(experiment_id)

    table = tracker.compare([experiment_id])
    result = table.results[0]
    assert result.finished_at is None
    assert result.metrics == {}
