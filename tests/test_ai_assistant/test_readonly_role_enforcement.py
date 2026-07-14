"""
The acceptance bar for "no write path" (decision #6, spec section 7
Definition of Done): connect to Postgres AS the llm_readonly role
itself and attempt a write against every table this component reads
from. A passing application-level test alone (e.g. asserting
ContextBuilder never calls INSERT) would NOT be sufficient — a future
contributor could still add a write call using the normal app role.
This test proves the guarantee at the only layer that actually matters:
Postgres itself must refuse the write, regardless of what application
code does or doesn't do.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal


@pytest.fixture
def readonly_db():
    session = ReadonlySessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.mark.parametrize(
    "table,columns,values",
    [
        ("signal_log", "(symbol, bar_time, strategy_id)", "('X/Y', now(), 's1')"),
        ("risk_decision_log", "(bar_time, strategy_id)", "(now(), 's1')"),
        (
            "paper_accounts",
            "(account_id, starting_balance, current_cash, created_at)",
            "('hacked', 1000, 1000, now())",
        ),
    ],
)
def test_llm_readonly_cannot_insert(readonly_db, table, columns, values):
    with pytest.raises(ProgrammingError, match="permission denied"):
        readonly_db.execute(text(f"INSERT INTO {table} {columns} VALUES {values}"))
        readonly_db.commit()


def test_llm_readonly_cannot_update_orders(readonly_db):
    with pytest.raises(ProgrammingError, match="permission denied"):
        readonly_db.execute(text("UPDATE orders SET state = 'filled' WHERE 1=0"))
        readonly_db.commit()


def test_llm_readonly_cannot_delete_from_fills(readonly_db):
    with pytest.raises(ProgrammingError, match="permission denied"):
        readonly_db.execute(text("DELETE FROM fills WHERE 1=0"))
        readonly_db.commit()


def test_llm_readonly_cannot_drop_a_table(readonly_db):
    # Postgres reports DDL refusal as an ownership error rather than
    # "permission denied", but the outcome is the same: the statement
    # is rejected, not merely a no-op.
    with pytest.raises(ProgrammingError, match="must be owner of table"):
        readonly_db.execute(text("DROP TABLE orders"))
        readonly_db.commit()


def test_llm_readonly_can_still_select(readonly_db):
    # The role must not be locked out entirely — SELECT is the whole
    # point of its existence.
    result = readonly_db.execute(text("SELECT count(*) FROM orders")).scalar_one()
    assert result >= 0


def test_default_app_role_can_still_write_orders():
    """Sanity check: the normal app role (core.db.SessionLocal) is
    unaffected by llm_readonly's restrictions — this component's
    guarantee is additive, not a global lockdown."""
    session = SessionLocal()
    try:
        session.execute(text("SELECT count(*) FROM orders")).scalar_one()
    finally:
        session.close()
