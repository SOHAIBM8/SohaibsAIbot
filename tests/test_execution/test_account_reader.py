"""
Tests run against real local Postgres.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.execution.account_reader import AccountReader

ACCOUNT_ID = "test_account_reader_account"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM account_snapshots WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.commit()
        session.close()


def _seed_account(db):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 95000, :t)
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )


def test_get_account_returns_none_when_missing(db):
    assert AccountReader(db).get_account("no-such-account") is None


def test_get_account_returns_record(db):
    _seed_account(db)
    db.commit()

    record = AccountReader(db).get_account(ACCOUNT_ID)

    assert record is not None
    assert record.starting_balance == 100000
    assert record.current_cash == 95000


def test_list_equity_curve_returns_empty_when_no_snapshots(db):
    _seed_account(db)
    db.commit()

    curve = AccountReader(db).list_equity_curve(ACCOUNT_ID)

    assert curve == []


def test_list_equity_curve_returns_snapshots_in_chronological_order(db):
    _seed_account(db)
    db.execute(
        text("""
            INSERT INTO account_snapshots (account_id, equity, open_position_count, snapshot_at)
            VALUES (:a, :equity, 0, :t)
            """),
        {"a": ACCOUNT_ID, "equity": 101000, "t": datetime(2024, 1, 2, tzinfo=UTC)},
    )
    db.execute(
        text("""
            INSERT INTO account_snapshots (account_id, equity, open_position_count, snapshot_at)
            VALUES (:a, :equity, 1, :t)
            """),
        {"a": ACCOUNT_ID, "equity": 100000, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.commit()

    curve = AccountReader(db).list_equity_curve(ACCOUNT_ID)

    assert [s.equity for s in curve] == [100000, 101000]
