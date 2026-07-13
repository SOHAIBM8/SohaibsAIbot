"""
Structure mirrors tests/test_regime_detector.py's hysteresis tests
almost exactly, per the spec's testing strategy — but CircuitBreaker's
hysteresis is asymmetric (trip is immediate, only clearing needs
confirmation_bars consecutive clean readings), so the "single reading"
tests assert immediate trip rather than "not yet confirmed."
"""

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.risk.circuit_breaker import CircuitBreaker, record_circuit_breaker_event


@pytest.fixture
def breaker():
    return CircuitBreaker(name="test_breaker", threshold=0.95, confirmation_bars=3)


def test_starts_not_tripped(breaker):
    assert breaker.evaluate(0.5) is False


def test_single_breach_trips_immediately(breaker):
    """Unlike RegimeDetector's symmetric hysteresis, a circuit breaker
    trips on the first bad reading — it's a fail-fast safety gate, not
    a noise filter on the way in."""
    assert breaker.evaluate(0.96) is True


def test_does_not_clear_on_one_clean_reading(breaker):
    breaker.evaluate(0.96)  # trip
    assert breaker.evaluate(0.5) is True  # still tripped, only 1 clean reading


def test_clears_after_confirmation_bars_consecutive_clean_readings(breaker):
    breaker.evaluate(0.96)  # trip
    for _ in range(2):
        assert breaker.evaluate(0.5) is True  # still pending
    assert breaker.evaluate(0.5) is False  # 3rd consecutive clean reading clears it


def test_flapping_readings_never_clear_the_breaker(breaker):
    """Alternating breach/clean readings should never accumulate 3
    consecutive clean readings — this is exactly the scenario the
    clearing hysteresis exists to prevent."""
    breaker.evaluate(0.96)  # trip
    for _ in range(10):
        assert breaker.evaluate(0.5) is True
        assert breaker.evaluate(0.96) is True


def test_reset_clears_tripped_and_pending_state(breaker):
    breaker.evaluate(0.96)
    breaker.evaluate(0.5)
    breaker.evaluate(0.5)

    breaker.reset()

    assert breaker.evaluate(0.5) is False  # back to normal, no leaked pending count
    for _ in range(2):
        assert breaker.evaluate(0.5) is False


def test_evaluate_detailed_reports_transition_on_trip(breaker):
    result = breaker.evaluate_detailed(0.96)
    assert result.tripped is True
    assert result.transitioned is True
    assert result.event_type == "tripped"


def test_evaluate_detailed_reports_no_transition_while_still_tripped(breaker):
    breaker.evaluate(0.96)
    result = breaker.evaluate_detailed(0.97)
    assert result.tripped is True
    assert result.transitioned is False
    assert result.event_type is None


def test_evaluate_detailed_reports_transition_on_clear(breaker):
    breaker.evaluate(0.96)
    breaker.evaluate(0.5)
    breaker.evaluate(0.5)
    result = breaker.evaluate_detailed(0.5)
    assert result.tripped is False
    assert result.transitioned is True
    assert result.event_type == "cleared"


def test_threshold_boundary_is_inclusive(breaker):
    assert breaker.evaluate(0.95) is True  # exactly at threshold trips


# --- persistence (real Postgres, not mocked) --------------------------------


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM circuit_breaker_event_log WHERE breaker_name = 'test_persisted'")
        )
        session.commit()
        session.close()


def test_record_circuit_breaker_event_persists_a_row(db):
    record_circuit_breaker_event(
        db, breaker_name="test_persisted", event_type="tripped", reason="value=0.96 >= 0.95"
    )
    row = (
        db.execute(
            text(
                "SELECT breaker_name, event_type, reason FROM circuit_breaker_event_log "
                "WHERE breaker_name = 'test_persisted'"
            )
        )
        .mappings()
        .first()
    )
    assert row["event_type"] == "tripped"
    assert row["reason"] == "value=0.96 >= 0.95"
