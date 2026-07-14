"""
PermissionValidator (step 5) depends on a Disarmer Protocol, never
importing ArmingService directly — this test proves the REAL
ArmingService (step 6) actually satisfies that Protocol end-to-end,
not just structurally. Runs against real local Postgres.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.security.arming_service import ArmingService
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient
from core.security.permission_checker import PermissionCheckResult
from core.security.permission_validator import PermissionValidator

ACCOUNT_ID = "test_pv_arming_account"
STRATEGY_ID = "test_pv_arming_strategy"
EXCHANGE = "binance"


class FakePermissionChecker:
    def check_permissions(self, api_key, api_secret) -> PermissionCheckResult:
        return PermissionCheckResult(withdrawals_enabled=True, raw={"enableWithdrawals": True})


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM arming_state WHERE account_id = :a"), {"a": ACCOUNT_ID})
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


def test_a_compromised_looking_credential_disarms_a_real_armed_strategy(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_PV_ARMING_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange=EXCHANGE, api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.ACTIVE)

    arming = ArmingService(db)
    arming.arm(ACCOUNT_ID, STRATEGY_ID, EXCHANGE, armed_by="alice", mainnet=False)
    assert arming.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is True

    provider = CredentialProvider(manager, vault, audit_db)
    validator = PermissionValidator(manager, provider, FakePermissionChecker(), disarmer=arming)

    validator.validate(credential_id)

    assert manager.get(credential_id).state == CredentialState.VALIDATION_FAILED
    assert arming.is_armed(ACCOUNT_ID, STRATEGY_ID, EXCHANGE) is False
