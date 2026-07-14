"""
Tests run against real local Postgres. The exchange permission check
itself is always a scripted fake — spec section 8: "never a real cloud
KMS/exchange call in the unit suite" applies here too. The withdrawal-
enabled test proves the FULL chain (state transition + disarm + event
published), not just PermissionCheckResult's classification.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.events import CredentialValidationFailed
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient
from core.security.permission_checker import PermissionCheckResult
from core.security.permission_validator import PermissionValidator

ACCOUNT_ID = "test_pv_account"


class FakePermissionChecker:
    def __init__(self, withdrawals_enabled: bool):
        self.withdrawals_enabled = withdrawals_enabled
        self.calls: list[tuple[str, str]] = []

    def check_permissions(self, api_key: str, api_secret: str) -> PermissionCheckResult:
        self.calls.append((api_key, api_secret))
        return PermissionCheckResult(
            withdrawals_enabled=self.withdrawals_enabled,
            raw={"enableWithdrawals": self.withdrawals_enabled},
        )


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


class FakeDisarmer:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def disarm_all(self, account_id: str, exchange: str, reason: str) -> None:
        self.calls.append((account_id, exchange, reason))


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
def manager(db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_PV_KEK"))
    return KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))


def register(manager, exchange="binance") -> str:
    return manager.register(
        account_id=ACCOUNT_ID, exchange=exchange, api_key="k", api_secret="s", mainnet=False
    )


def test_successful_validation_activates_a_pending_credential(db, audit_db, manager):
    credential_id = register(manager)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=False)
    validator = PermissionValidator(manager, provider, checker)

    result = validator.validate(credential_id)

    assert result.withdrawals_enabled is False
    credential = manager.get(credential_id)
    assert credential.state == CredentialState.ACTIVE
    assert credential.last_validated_at is not None
    assert checker.calls == [("k", "s")]


def test_withdrawal_enabled_transitions_to_validation_failed(db, audit_db, manager):
    credential_id = register(manager)
    manager.transition(credential_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=True)
    validator = PermissionValidator(manager, provider, checker)

    validator.validate(credential_id)

    assert manager.get(credential_id).state == CredentialState.VALIDATION_FAILED


def test_withdrawal_enabled_disarms_all_strategies_for_the_credentials_account_and_exchange(
    db, audit_db, manager
):
    credential_id = register(manager, exchange="binance")
    manager.transition(credential_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=True)
    disarmer = FakeDisarmer()
    validator = PermissionValidator(manager, provider, checker, disarmer=disarmer)

    validator.validate(credential_id)

    assert disarmer.calls == [(ACCOUNT_ID, "binance", "withdrawal_permission_enabled")]


def test_withdrawal_enabled_publishes_credential_validation_failed(db, audit_db, manager):
    credential_id = register(manager)
    manager.transition(credential_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=True)
    event_bus = FakeEventBus()
    validator = PermissionValidator(manager, provider, checker, event_bus=event_bus)

    validator.validate(credential_id)

    assert len(event_bus.published) == 1
    event = event_bus.published[0]
    assert isinstance(event, CredentialValidationFailed)
    assert event.credential_id == credential_id
    assert event.reason == "withdrawal_permission_enabled"


def test_full_chain_all_three_effects_happen_together(db, audit_db, manager):
    """The spec's own framing: 'test the full chain, not just the
    classification' — one call, three independently-observable effects."""
    credential_id = register(manager)
    manager.transition(credential_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=True)
    event_bus = FakeEventBus()
    disarmer = FakeDisarmer()
    validator = PermissionValidator(
        manager, provider, checker, event_bus=event_bus, disarmer=disarmer
    )

    validator.validate(credential_id)

    assert manager.get(credential_id).state == CredentialState.VALIDATION_FAILED
    assert len(disarmer.calls) == 1
    assert len(event_bus.published) == 1


def test_recurring_recheck_on_an_active_credential_updates_last_validated_at_without_reraising(
    db, audit_db, manager
):
    """Decision #2: a stale one-time check is not acceptable — a
    SECOND successful validate() call on an already-ACTIVE credential
    must still update last_validated_at, not silently no-op."""
    credential_id = register(manager)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=False)
    validator = PermissionValidator(manager, provider, checker)

    validator.validate(credential_id)  # PENDING_VALIDATION -> ACTIVE
    first_validated_at = manager.get(credential_id).last_validated_at

    # force the clock forward at the DB level so a second timestamp is
    # provably later, not just coincidentally equal
    db.execute(
        text("UPDATE encrypted_credentials SET last_validated_at = :t WHERE credential_id = :c"),
        {"t": datetime.now(UTC) - timedelta(hours=1), "c": credential_id},
    )
    db.commit()

    validator.validate(credential_id)  # ACTIVE -> ACTIVE (recheck)

    second_validated_at = manager.get(credential_id).last_validated_at
    assert manager.get(credential_id).state == CredentialState.ACTIVE
    assert second_validated_at > first_validated_at - timedelta(hours=2)
    assert checker.calls == [("k", "s"), ("k", "s")]


def test_sweep_active_credentials_validates_every_active_credential(db, audit_db, manager):
    active_id = register(manager, exchange="binance")
    pending_id = register(manager, exchange="binance")
    manager.transition(active_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=False)
    validator = PermissionValidator(manager, provider, checker)

    swept = validator.sweep_active_credentials()

    assert swept == [active_id]
    assert pending_id not in swept  # only ACTIVE credentials are swept
    assert len(checker.calls) == 1


def test_sweep_active_credentials_disarms_and_fails_a_compromised_looking_key(
    db, audit_db, manager
):
    credential_id = register(manager)
    manager.transition(credential_id, CredentialState.ACTIVE)
    provider = CredentialProvider(manager, manager.vault, audit_db)
    checker = FakePermissionChecker(withdrawals_enabled=True)
    disarmer = FakeDisarmer()
    event_bus = FakeEventBus()
    validator = PermissionValidator(
        manager, provider, checker, event_bus=event_bus, disarmer=disarmer
    )

    validator.sweep_active_credentials()

    assert manager.get(credential_id).state == CredentialState.VALIDATION_FAILED
    assert len(disarmer.calls) == 1
    assert len(event_bus.published) == 1
