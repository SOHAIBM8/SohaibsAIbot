"""
Tests run against real local Postgres, including a REAL KillSwitch
(core.risk.kill_switch) for the dual-gate test — not a fake, since the
entire point is proving two independently-real components combine
correctly.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.risk.kill_switch import KillSwitch
from core.security.arming_service import ArmingService, is_trading_permitted
from core.security.events import ArmingStateChanged

ACCOUNT_ID = "test_arming_account"
STRATEGY_ID = "test_arming_strategy"
EXCHANGE = "binance"


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM arming_state WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.execute(text("DELETE FROM kill_switch_state WHERE scope LIKE 'test_arming_%'"))
        session.commit()
        session.close()


def test_arm_then_is_armed_is_true(db):
    service = ArmingService(db, arming_duration=timedelta(hours=48))
    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)

    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is True


def test_never_armed_is_not_armed(db):
    service = ArmingService(db)
    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False


def test_disarm_makes_is_armed_false(db):
    service = ArmingService(db)
    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    service.disarm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, reason="manual")

    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False


def test_arm_publishes_arming_state_changed(db):
    event_bus = FakeEventBus()
    service = ArmingService(db, event_bus=event_bus)

    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)

    assert len(event_bus.published) == 1
    assert isinstance(event_bus.published[0], ArmingStateChanged)
    assert event_bus.published[0].armed is True


def test_expiry_makes_is_armed_false_without_any_explicit_disarm_call(db):
    """The spec's own framing, verbatim: simulated time passes
    expires_at — is_armed() returns False with no disarm() call."""
    service = ArmingService(db, arming_duration=timedelta(hours=48))
    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)

    just_before_expiry = datetime.now(UTC) + timedelta(hours=47, minutes=59)
    just_after_expiry = datetime.now(UTC) + timedelta(hours=48, minutes=1)

    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, now=just_before_expiry) is True
    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, now=just_after_expiry) is False


def test_config_change_reverts_an_armed_strategy_to_unarmed(db):
    service = ArmingService(db)
    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is True

    service.on_config_changed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE)

    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False


def test_re_arming_after_a_config_change_requires_a_fresh_arm_call(db):
    service = ArmingService(db)
    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    service.on_config_changed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE)
    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False

    service.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    assert service.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is True


def test_disarm_all_disarms_every_strategy_for_the_account_and_exchange(db):
    service = ArmingService(db)
    service.arm(ACCOUNT_ID, "strategy_a", EXCHANGE, armed_by="alice", mainnet=False)
    service.arm(ACCOUNT_ID, "strategy_b", EXCHANGE, armed_by="alice", mainnet=False)
    service.arm(
        ACCOUNT_ID, "strategy_c", "kraken", armed_by="alice", mainnet=False
    )  # different exchange

    service.disarm_all(ACCOUNT_ID, EXCHANGE, reason="withdrawal_permission_enabled")

    assert service.is_armed(ACCOUNT_ID, "strategy_a", EXCHANGE) is False
    assert service.is_armed(ACCOUNT_ID, "strategy_b", EXCHANGE) is False
    assert (
        service.is_armed(ACCOUNT_ID, "strategy_c", "kraken") is True
    )  # untouched — different exchange

    db.execute(text("DELETE FROM arming_state WHERE strategy_id = 'strategy_c'"))
    db.commit()


def test_disarm_all_with_nothing_armed_is_a_no_op(db):
    service = ArmingService(db)
    service.disarm_all(ACCOUNT_ID, EXCHANGE, reason="withdrawal_permission_enabled")
    # must not raise


# --- dual-gate test: all three states explicitly ------------------------


def test_dual_gate_kill_switch_engaged_and_arming_active_is_blocked(db):
    kill_switch = KillSwitch(db, scope="test_arming_dual_1")
    kill_switch.engage(reason="test", engaged_by="test_suite")
    arming = ArmingService(db)
    arming.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)

    assert is_trading_permitted(kill_switch, arming, ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False


def test_dual_gate_kill_switch_clear_and_arming_expired_is_blocked(db):
    kill_switch = KillSwitch(db, scope="test_arming_dual_2")
    arming = ArmingService(db, arming_duration=timedelta(hours=48))
    arming.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    well_after_expiry = datetime.now(UTC) + timedelta(hours=49)

    assert (
        is_trading_permitted(
            kill_switch, arming, ACCOUNT_ID, STRATEGY_ID, EXCHANGE, now=well_after_expiry
        )
        is False
    )


def test_dual_gate_both_clear_and_active_proceeds(db):
    kill_switch = KillSwitch(db, scope="test_arming_dual_3")
    arming = ArmingService(db)
    arming.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)

    assert is_trading_permitted(kill_switch, arming, ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is True


def test_dual_gate_never_armed_at_all_is_blocked(db):
    kill_switch = KillSwitch(db, scope="test_arming_dual_4")
    arming = ArmingService(db)

    assert is_trading_permitted(kill_switch, arming, ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False
