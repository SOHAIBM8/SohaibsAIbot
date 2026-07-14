"""
Decision #8, treated as absolute (spec section 8): run the full
decrypt-and-audit path with structlog's output captured, and assert
the known plaintext test credential value never appears anywhere in
the captured log stream — at any level, in any field, not even
truncated.

Uses structlog.testing.capture_logs(), which captures every log call
that goes through structlog's machinery regardless of configured
processors/output level — this test is not fooled by, e.g., an INFO-
level filter hiding a plaintext value that only appears at DEBUG.
"""

from datetime import timedelta

import pytest
import structlog
from sqlalchemy import text

from core.db import SessionLocal
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_no_plaintext_account"
PLAINTEXT_API_KEY = "PLAINTEXT-SENTINEL-API-KEY-4f9c2e"
PLAINTEXT_API_SECRET = "PLAINTEXT-SENTINEL-API-SECRET-8b1a77"


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


def _captured_text(captured_logs: list[dict]) -> str:
    """Flatten every field of every captured log event into one
    string, so a single substring check covers the entire log stream
    regardless of which field/level it might have leaked into."""
    return "\n".join(str(entry) for entry in captured_logs)


def test_registration_and_decryption_never_log_the_plaintext(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_NPL_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    provider = CredentialProvider(manager, vault, audit_db)

    with structlog.testing.capture_logs() as captured:
        credential_id = manager.register(
            account_id=ACCOUNT_ID,
            exchange="binance",
            api_key=PLAINTEXT_API_KEY,
            api_secret=PLAINTEXT_API_SECRET,
            mainnet=False,
        )
        manager.transition(credential_id, CredentialState.ACTIVE)
        credentials = provider.get_credentials(
            credential_id, requested_by="test_suite", client_order_id="co-1"
        )

    # Sanity: the decrypt path genuinely produced the real plaintext —
    # this test would be meaningless if it never actually appeared in
    # memory at all.
    assert credentials.api_key == PLAINTEXT_API_KEY
    assert credentials.api_secret == PLAINTEXT_API_SECRET

    log_text = _captured_text(captured)
    assert PLAINTEXT_API_KEY not in log_text
    assert PLAINTEXT_API_SECRET not in log_text


def test_error_path_never_logs_the_plaintext_either(db, audit_db):
    """A malformed/unexpected condition is exactly when a careless
    implementation tends to dump extra context "to help debugging" —
    proving the error path is clean too, not just the happy path."""
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_NPL_KEK2"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    provider = CredentialProvider(manager, vault, audit_db)
    credential_id = manager.register(
        account_id=ACCOUNT_ID,
        exchange="binance",
        api_key=PLAINTEXT_API_KEY,
        api_secret=PLAINTEXT_API_SECRET,
        mainnet=False,
    )

    with structlog.testing.capture_logs() as captured:
        provider.get_credentials(credential_id, requested_by="test_suite")
        try:
            # illegal from PENDING_VALIDATION
            manager.transition(credential_id, CredentialState.ROTATION_DUE)
        except ValueError:
            pass

    log_text = _captured_text(captured)
    assert PLAINTEXT_API_KEY not in log_text
    assert PLAINTEXT_API_SECRET not in log_text
