"""
Tests run against real local Postgres. risk_decision_log rows are
seeded via direct SQL rather than through RiskEngine — RiskDecisionLogReader
only needs to prove it maps arbitrary persisted rows back correctly,
independent of how they got there.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.risk.risk_decision import RiskDecisionLogReader

STRATEGY_ID = "test_reader_strategy"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID}
        )
        session.commit()
        session.close()


def _insert_decision(db, bar_time, **overrides):
    defaults = dict(
        strategy_id=STRATEGY_ID,
        proposed_quantity=1.0,
        approved_quantity=0.5,
        rejection_reason=None,
        throttle_reasons=["drawdown_tier_reduction"],
        layer_results=json.dumps(
            [{"layer_name": "budget", "passed": True, "multiplier": 0.5, "reason": "tier 1"}]
        ),
        risk_config_id=None,
    )
    defaults.update(overrides)
    return db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity,
                 rejection_reason, throttle_reasons, layer_results, risk_config_id)
            VALUES
                (:bar_time, :strategy_id, :proposed_quantity, :approved_quantity,
                 :rejection_reason, :throttle_reasons, :layer_results, :risk_config_id)
            RETURNING id
            """),
        {"bar_time": bar_time, **defaults},
    ).scalar_one()


def test_list_recent_returns_most_recent_first(db):
    id_a = _insert_decision(db, datetime(2024, 1, 1, tzinfo=UTC))
    id_b = _insert_decision(db, datetime(2024, 1, 2, tzinfo=UTC))
    db.commit()

    results = RiskDecisionLogReader(db).list_recent(limit=10)

    ids_in_order = [r.id for r in results if r.id in (id_a, id_b)]
    assert ids_in_order == [id_b, id_a]


def test_list_recent_maps_layer_results_and_throttle_reasons(db):
    decision_id = _insert_decision(db, datetime(2024, 1, 1, tzinfo=UTC))
    db.commit()

    result = next(r for r in RiskDecisionLogReader(db).list_recent(limit=10) if r.id == decision_id)

    assert result.strategy_id == STRATEGY_ID
    assert result.approved_quantity == 0.5
    assert len(result.layer_results) == 1
    assert result.layer_results[0].layer_name == "budget"
    assert result.layer_results[0].reason == "tier 1"
    assert result.throttle_reasons[0].value == "drawdown_tier_reduction"
    assert result.rejection_reason is None


def test_list_recent_maps_rejection_reason_when_present(db):
    decision_id = _insert_decision(
        db,
        datetime(2024, 1, 1, tzinfo=UTC),
        rejection_reason="kill_switch_active",
        approved_quantity=0.0,
    )
    db.commit()

    result = next(r for r in RiskDecisionLogReader(db).list_recent(limit=10) if r.id == decision_id)

    assert result.rejection_reason is not None
    assert result.rejection_reason.value == "kill_switch_active"


def test_list_recent_respects_limit(db):
    for i in range(3):
        _insert_decision(db, datetime(2024, 1, i + 1, tzinfo=UTC))
    db.commit()

    results = RiskDecisionLogReader(db).list_recent(limit=1)

    assert len(results) == 1
