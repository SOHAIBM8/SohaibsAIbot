"""
Risk monitoring API integration tests against real local Postgres.
Seeds via the same core classes the route wraps (KillSwitch, direct
SQL for circuit_breaker_event_log/risk_decision_log — no core reader
class exists to seed those, only to read them), never mocks.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.risk.circuit_breaker import record_circuit_breaker_event
from core.risk.kill_switch import KillSwitch
from core.security.arming_service import ArmingService
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

TEST_STRATEGY_ID = "test_api_risk_strategy"
ACCOUNT_ID = "test_dashboard_account"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return response.cookies["dashboard_csrf"]


def test_risk_config_requires_auth(client):
    response = client.get("/api/risk/config")
    assert response.status_code == 401


def test_risk_config_returns_yaml_backed_values(client):
    _logged_in(client)
    response = client.get("/api/risk/config")
    assert response.status_code == 200
    body = response.json()
    assert body["risk_config_id"] == "default"
    assert "daily_loss_limit_pct" in body


@pytest.fixture
def cleanup_kill_switch(db):
    yield
    db.execute(text("DELETE FROM kill_switch_state WHERE scope = 'global'"))
    db.commit()


def test_kill_switch_state_defaults_to_disengaged(client, cleanup_kill_switch):
    _logged_in(client)
    response = client.get("/api/risk/kill-switch")
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "global"
    assert body["engaged"] is False


def test_kill_switch_state_reflects_engaged_row(client, db, cleanup_kill_switch):
    KillSwitch(db, scope="global").engage(reason="test halt", engaged_by="tester")
    _logged_in(client)

    response = client.get("/api/risk/kill-switch")

    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is True
    assert body["engaged_reason"] == "test halt"
    assert body["engaged_by"] == "tester"


@pytest.fixture
def cleanup_circuit_breakers(db):
    yield
    db.execute(
        text("DELETE FROM circuit_breaker_event_log WHERE breaker_name = 'test_api_breaker'")
    )
    db.commit()


def test_circuit_breakers_reflects_latest_event(client, db, cleanup_circuit_breakers):
    record_circuit_breaker_event(
        db, breaker_name="test_api_breaker", event_type="tripped", reason="atr spike"
    )
    _logged_in(client)

    response = client.get("/api/risk/circuit-breakers")

    assert response.status_code == 200
    by_name = {row["breaker_name"]: row for row in response.json()}
    assert by_name["test_api_breaker"]["tripped"] is True
    assert by_name["test_api_breaker"]["reason"] == "atr spike"


@pytest.fixture
def cleanup_decisions(db):
    yield
    db.execute(
        text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": TEST_STRATEGY_ID}
    )
    db.commit()


def test_recent_decisions_returns_seeded_row(client, db, cleanup_decisions):
    db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity,
                 rejection_reason, throttle_reasons, layer_results, risk_config_id)
            VALUES
                (:bar_time, :strategy_id, 1.0, 0.5, NULL,
                 ARRAY['drawdown_tier_reduction'], :layer_results, NULL)
            """),
        {
            "bar_time": datetime(2024, 1, 1, tzinfo=UTC),
            "strategy_id": TEST_STRATEGY_ID,
            "layer_results": json.dumps(
                [{"layer_name": "budget", "passed": True, "multiplier": 0.5, "reason": "tier 1"}]
            ),
        },
    )
    db.commit()
    _logged_in(client)

    response = client.get("/api/risk/decisions", params={"limit": 50})

    assert response.status_code == 200
    matching = [row for row in response.json() if row["strategy_id"] == TEST_STRATEGY_ID]
    assert len(matching) == 1
    assert matching[0]["approved_quantity"] == 0.5
    assert matching[0]["throttle_reasons"] == ["drawdown_tier_reduction"]
    assert matching[0]["layer_results"][0]["layer_name"] == "budget"


@pytest.fixture
def cleanup_arming(db):
    yield
    db.execute(
        text("DELETE FROM arming_state WHERE account_id = :a AND strategy_id = :s"),
        {"a": ACCOUNT_ID, "s": TEST_STRATEGY_ID},
    )
    db.commit()


def test_arming_state_requires_auth(client):
    response = client.get(
        "/api/risk/arming", params={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance"}
    )
    assert response.status_code == 401


def test_arming_state_404_when_never_armed(client, cleanup_arming):
    _logged_in(client)
    response = client.get(
        "/api/risk/arming", params={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance"}
    )
    assert response.status_code == 404


def test_arming_state_reflects_armed_row(client, db, cleanup_arming):
    ArmingService(db).arm(
        account_id=ACCOUNT_ID,
        strategy_id=TEST_STRATEGY_ID,
        exchange="binance",
        armed_by="tester",
        mainnet=False,
    )
    _logged_in(client)

    response = client.get(
        "/api/risk/arming", params={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["armed"] is True
    assert body["armed_by"] == "tester"
    assert body["account_id"] == ACCOUNT_ID


# --- control surfaces (mutating) ---------------------------------------


def test_engage_kill_switch_requires_auth(client):
    response = client.post("/api/risk/kill-switch/engage", json={"reason": "test"})
    assert response.status_code == 401


def test_engage_kill_switch_requires_csrf(client, cleanup_kill_switch):
    _logged_in(client)
    response = client.post("/api/risk/kill-switch/engage", json={"reason": "test"})
    assert response.status_code == 403


def test_engage_then_disengage_kill_switch(client, db, cleanup_kill_switch):
    csrf_token = _logged_in(client)

    engage_response = client.post(
        "/api/risk/kill-switch/engage",
        json={"reason": "manual test halt"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert engage_response.status_code == 200
    body = engage_response.json()
    assert body["engaged"] is True
    assert body["engaged_reason"] == "manual test halt"
    assert body["engaged_by"] == TEST_OPERATOR_USERNAME

    disengage_response = client.post(
        "/api/risk/kill-switch/disengage", headers={"X-CSRF-Token": csrf_token}
    )
    assert disengage_response.status_code == 200
    assert disengage_response.json()["engaged"] is False


def test_engage_kill_switch_rejects_empty_reason(client, cleanup_kill_switch):
    csrf_token = _logged_in(client)
    response = client.post(
        "/api/risk/kill-switch/engage",
        json={"reason": ""},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 422


def test_arm_requires_csrf(client):
    _logged_in(client)
    response = client.post(
        "/api/risk/arming/arm",
        json={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance", "mainnet": False},
    )
    assert response.status_code == 403


def test_arm_rejects_mainnet(client, cleanup_arming):
    csrf_token = _logged_in(client)
    response = client.post(
        "/api/risk/arming/arm",
        json={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance", "mainnet": True},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 400


def test_arm_then_disarm(client, cleanup_arming):
    csrf_token = _logged_in(client)

    arm_response = client.post(
        "/api/risk/arming/arm",
        json={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance", "mainnet": False},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert arm_response.status_code == 200
    body = arm_response.json()
    assert body["armed"] is True
    assert body["mainnet"] is False
    assert body["armed_by"] == TEST_OPERATOR_USERNAME

    disarm_response = client.post(
        "/api/risk/arming/disarm",
        json={"strategy_id": TEST_STRATEGY_ID, "exchange": "binance", "reason": "test done"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert disarm_response.status_code == 200
    assert disarm_response.json()["armed"] is False


def test_disarm_unknown_record_is_404(client, cleanup_arming):
    csrf_token = _logged_in(client)
    response = client.post(
        "/api/risk/arming/disarm",
        json={"strategy_id": "test_never_armed_strategy", "exchange": "binance", "reason": "x"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 404
