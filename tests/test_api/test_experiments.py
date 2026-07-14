"""
Experiments API integration tests against real local Postgres — seeds
rows via the real ExperimentTracker (the same core module the route
wraps), not raw SQL, so these tests exercise the exact same read path
production traffic would.
"""

import pytest
from sqlalchemy import text

from core.experiment import ExperimentConfig, ExperimentTracker
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME


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


@pytest.fixture
def seeded_experiments(db):
    tracker = ExperimentTracker(db)
    id_a = tracker.start(make_config(code_commit_hash="commit_a"))
    id_b = tracker.start(make_config(code_commit_hash="commit_b"))
    tracker.finish(id_a, metrics={"sharpe": 1.0}, equity_curve_path="a.parquet")
    tracker.finish(id_b, metrics={"sharpe": 2.0}, equity_curve_path="b.parquet")
    yield id_a, id_b
    db.execute(
        text("DELETE FROM experiments WHERE experiment_id = ANY(:ids)"), {"ids": [id_a, id_b]}
    )
    db.commit()


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return client


def test_list_experiments_requires_auth(client):
    response = client.get("/api/experiments")
    assert response.status_code == 401


def test_list_experiments_returns_seeded_rows(client, seeded_experiments):
    id_a, id_b = seeded_experiments
    _logged_in(client)

    response = client.get("/api/experiments", params={"limit": 50})

    assert response.status_code == 200
    ids = [row["experiment_id"] for row in response.json()]
    assert id_a in ids
    assert id_b in ids


def test_get_experiment_by_id_returns_full_result(client, seeded_experiments):
    id_a, _ = seeded_experiments
    _logged_in(client)

    response = client.get(f"/api/experiments/{id_a}")

    assert response.status_code == 200
    body = response.json()
    assert body["experiment_id"] == id_a
    assert body["metrics"] == {"sharpe": 1.0}
    assert body["config"]["code_commit_hash"] == "commit_a"
    assert body["config"]["date_range"] == ["2024-01-01", "2024-06-01"]


def test_get_experiment_unknown_id_is_404(client):
    _logged_in(client)
    response = client.get("/api/experiments/-1")
    assert response.status_code == 404


def test_compare_returns_both_experiments_distinct(client, seeded_experiments):
    id_a, id_b = seeded_experiments
    _logged_in(client)

    response = client.get("/api/experiments/compare", params={"experiment_ids": [id_a, id_b]})

    assert response.status_code == 200
    body = response.json()
    by_id = {r["experiment_id"]: r for r in body["results"]}
    assert by_id[id_a]["metrics"]["sharpe"] == 1.0
    assert by_id[id_b]["metrics"]["sharpe"] == 2.0


def test_compare_requires_at_least_one_id(client):
    _logged_in(client)
    response = client.get("/api/experiments/compare", params={"experiment_ids": []})
    assert response.status_code == 422
