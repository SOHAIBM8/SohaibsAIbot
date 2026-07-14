"""
Tests run against real local Postgres: encrypted_credentials via the
normal app role (core.db.SessionLocal), the audit write via the real
INSERT-only credential_audit_writer role
(core.security.audit_db.AuditWriterSessionLocal) — exactly the
connection CredentialProvider actually uses in production, not a
mock of it.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_cp_account"


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
def audit_db():
    session = AuditWriterSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def registered_credential(db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_CP_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID,
        exchange="binance",
        api_key="test-plaintext-api-key",
        api_secret="test-plaintext-api-secret",
        mainnet=False,
    )
    return credential_id, manager, vault


def test_get_credentials_returns_the_original_plaintext(db, audit_db, registered_credential):
    credential_id, manager, vault = registered_credential
    provider = CredentialProvider(manager, vault, audit_db)

    credentials = provider.get_credentials(credential_id, requested_by="test_suite")

    assert credentials.api_key == "test-plaintext-api-key"
    assert credentials.api_secret == "test-plaintext-api-secret"


def test_get_credentials_writes_exactly_one_audit_row(db, audit_db, registered_credential):
    credential_id, manager, vault = registered_credential
    provider = CredentialProvider(manager, vault, audit_db)

    provider.get_credentials(credential_id, requested_by="test_suite", client_order_id="co-1")

    rows = (
        db.execute(
            text("""
            SELECT credential_id, action, requested_by, client_order_id
            FROM credential_audit_log WHERE credential_id = :c
            """),
            {"c": credential_id},
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "decrypted"
    assert rows[0]["requested_by"] == "test_suite"
    assert rows[0]["client_order_id"] == "co-1"


def test_get_credentials_audit_row_has_no_client_order_id_when_not_supplied(
    db, audit_db, registered_credential
):
    credential_id, manager, vault = registered_credential
    provider = CredentialProvider(manager, vault, audit_db)

    provider.get_credentials(credential_id, requested_by="test_suite")

    row = (
        db.execute(
            text("SELECT client_order_id FROM credential_audit_log WHERE credential_id = :c"),
            {"c": credential_id},
        )
        .mappings()
        .one()
    )
    assert row["client_order_id"] is None


def test_repeated_calls_each_write_their_own_audit_row(db, audit_db, registered_credential):
    """Every decrypt is audited — this is not a cached/idempotent call."""
    credential_id, manager, vault = registered_credential
    provider = CredentialProvider(manager, vault, audit_db)

    provider.get_credentials(credential_id, requested_by="test_suite")
    provider.get_credentials(credential_id, requested_by="test_suite")
    provider.get_credentials(credential_id, requested_by="test_suite")

    count = db.execute(
        text("SELECT count(*) FROM credential_audit_log WHERE credential_id = :c"),
        {"c": credential_id},
    ).scalar_one()
    assert count == 3


def test_get_credentials_raises_for_unknown_credential(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_CP_KEK2"))
    manager = KeyLifecycleManager(db, vault)
    provider = CredentialProvider(manager, vault, audit_db)

    with pytest.raises(KeyError):
        provider.get_credentials("does-not-exist", requested_by="test_suite")
