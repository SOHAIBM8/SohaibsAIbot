"""
The acceptance bar for decision #4 (spec section 8): connect to
Postgres AS credential_audit_writer and prove the database itself
refuses UPDATE/DELETE against credential_audit_log — the same pattern
as the AI assistant spec's test_readonly_role_enforcement.py, inverted
for a write-only table (INSERT allowed, everything else refused).

Dev-environment caveat (documented in schema.sql where the table/role
are defined): this project's bootstrap app role ("trading") is a
Postgres SUPERUSER in this local docker-compose setup, confirmed via
`SELECT rolsuper FROM pg_roles`. Superusers bypass every ACL check
unconditionally, so no GRANT/REVOKE could make "trading cannot
UPDATE/DELETE this table" true here — asserting that would be false,
not a real security boundary. What IS genuinely, meaningfully enforced
and tested below is credential_audit_writer itself: a real,
non-superuser role, which is the actual connection CredentialProvider
uses to write this table in production.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from core.db import SessionLocal
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_audit_immutability_account"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("""
            DELETE FROM credential_audit_log WHERE credential_id IN
                (SELECT credential_id FROM encrypted_credentials WHERE account_id = :a)
            """),
            {"a": ACCOUNT_ID},
        )
        session.execute(
            text("DELETE FROM encrypted_credentials WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
        session.commit()
        session.close()


@pytest.fixture
def audit_writer_db():
    session = AuditWriterSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def seeded_audit_row(db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_AIL_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    db.execute(
        text("""
            INSERT INTO credential_audit_log (credential_id, action, requested_by, occurred_at)
            VALUES (:c, 'decrypted', 'seed', :now)
            """),
        {"c": credential_id, "now": datetime.now(UTC)},
    )
    db.commit()
    return credential_id


def test_credential_audit_writer_can_insert(audit_writer_db, seeded_audit_row):
    credential_id = seeded_audit_row
    audit_writer_db.execute(
        text("""
            INSERT INTO credential_audit_log (credential_id, action, requested_by, occurred_at)
            VALUES (:c, 'decrypted', 'test_writer', :now)
            """),
        {"c": credential_id, "now": datetime.now(UTC)},
    )
    audit_writer_db.commit()  # must not raise — INSERT is exactly what this role is for


def test_credential_audit_writer_cannot_update(audit_writer_db, seeded_audit_row):
    with pytest.raises(ProgrammingError, match="permission denied"):
        audit_writer_db.execute(
            text("UPDATE credential_audit_log SET requested_by = 'tampered' WHERE 1=1")
        )
        audit_writer_db.commit()


def test_credential_audit_writer_cannot_delete(audit_writer_db, seeded_audit_row):
    with pytest.raises(ProgrammingError, match="permission denied"):
        audit_writer_db.execute(text("DELETE FROM credential_audit_log WHERE 1=1"))
        audit_writer_db.commit()


def test_credential_audit_writer_cannot_select_from_it(audit_writer_db, seeded_audit_row):
    """INSERT-only means exactly that — this role isn't even granted
    SELECT on the table it writes to. A writer that can't read back
    what it wrote is the correct posture for a pure audit sink."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        audit_writer_db.execute(text("SELECT * FROM credential_audit_log"))


def test_credential_audit_writer_cannot_drop_the_table(audit_writer_db, seeded_audit_row):
    with pytest.raises(ProgrammingError, match="must be owner of table"):
        audit_writer_db.execute(text("DROP TABLE credential_audit_log"))
        audit_writer_db.commit()
