"""
Tests run against real local Postgres, not mocks — consistent with
every other DB-touching component in this project. Each test cleans
up the kill_switch_state row(s) it creates.
"""

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.risk.kill_switch import KillSwitch


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM kill_switch_state WHERE scope LIKE 'test_%'"))
        session.commit()
        session.close()


def test_is_engaged_defaults_false_with_no_prior_state(db):
    switch = KillSwitch(db, scope="test_fresh")
    assert switch.is_engaged() is False


def test_engage_sets_engaged_true(db):
    switch = KillSwitch(db, scope="test_engage")
    switch.engage(reason="manual halt", engaged_by="alice")
    assert switch.is_engaged() is True

    row = (
        db.execute(
            text(
                "SELECT engaged, engaged_reason, engaged_by FROM kill_switch_state WHERE scope = :s"
            ),
            {"s": "test_engage"},
        )
        .mappings()
        .first()
    )
    assert row["engaged"] is True
    assert row["engaged_reason"] == "manual halt"
    assert row["engaged_by"] == "alice"


def test_disengage_sets_engaged_false(db):
    switch = KillSwitch(db, scope="test_disengage")
    switch.engage(reason="halt", engaged_by="alice")
    switch.disengage(disengaged_by="bob")
    assert switch.is_engaged() is False

    row = (
        db.execute(
            text("SELECT engaged FROM kill_switch_state WHERE scope = :s"), {"s": "test_disengage"}
        )
        .mappings()
        .first()
    )
    assert row["engaged"] is False


def test_no_auto_clear_path_exists_on_the_class():
    # KillSwitch exposes exactly engage/disengage/is_engaged/load_state
    # — no timer, no scheduled task, nothing that could auto-clear an
    # engaged switch without an explicit disengage() call.
    public_methods = {name for name in dir(KillSwitch) if not name.startswith("_")}
    assert public_methods == {"is_engaged", "engage", "disengage", "load_state"}


def test_state_survives_reconstruction_simulating_a_process_restart(db):
    """The whole point of persisting kill switch state: a brand new
    KillSwitch instance, reading the same DB, must come up already
    engaged — not reset to False just because the process restarted."""
    first_process = KillSwitch(db, scope="test_restart")
    first_process.engage(reason="drawdown tier 3 breach", engaged_by="risk_engine")
    assert first_process.is_engaged() is True

    # Simulate a fresh process: a new instance, no shared in-memory state.
    second_process = KillSwitch(db, scope="test_restart")
    assert second_process.is_engaged() is True


def test_load_state_refreshes_from_db_after_external_change(db):
    switch = KillSwitch(db, scope="test_reload")
    assert switch.is_engaged() is False

    # Simulate another process engaging it directly in the DB.
    other = KillSwitch(db, scope="test_reload")
    other.engage(reason="halt", engaged_by="alice")

    assert switch.is_engaged() is False  # stale in-memory cache
    switch.load_state()
    assert switch.is_engaged() is True  # now reflects the DB


def test_scopes_are_independent(db):
    global_switch = KillSwitch(db, scope="test_scope_a")
    other_switch = KillSwitch(db, scope="test_scope_b")

    global_switch.engage(reason="halt", engaged_by="alice")

    assert global_switch.is_engaged() is True
    assert other_switch.is_engaged() is False


def test_reengage_updates_reason_and_engaged_by(db):
    switch = KillSwitch(db, scope="test_reengage")
    switch.engage(reason="first reason", engaged_by="alice")
    switch.engage(reason="second reason", engaged_by="bob")

    row = (
        db.execute(
            text("SELECT engaged_reason, engaged_by FROM kill_switch_state WHERE scope = :s"),
            {"s": "test_reengage"},
        )
        .mappings()
        .first()
    )
    assert row["engaged_reason"] == "second reason"
    assert row["engaged_by"] == "bob"
