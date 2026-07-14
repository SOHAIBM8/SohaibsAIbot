"""
Tests run against real local Postgres. Proves: (1) after revoke(),
every subsequent CredentialProvider.get_credentials() call fails until
an explicit re_grant(); (2) an order-placement call site never even
reaches its "place the order" step when credentials can't be obtained
— the ordering itself is the guarantee, not just an exception type.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.security.audit_db import AuditWriterSessionLocal
from core.security.credential_provider import CredentialProvider
from core.security.credential_vault import CredentialVault
from core.security.emergency_revocation import CredentialRevokedError, EmergencyCredentialRevocation
from core.security.events import EmergencyRevocationTriggered
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_emergency_account"


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
        session.execute(
            text("""
            DELETE FROM credential_revocation WHERE credential_id IN
                (SELECT credential_id FROM encrypted_credentials WHERE account_id = :a)
            """),
            {"a": ACCOUNT_ID},
        )
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
def setup(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_ECR_KEK"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.ACTIVE)
    revocation = EmergencyCredentialRevocation(db, manager)
    provider = CredentialProvider(manager, vault, audit_db, revocation=revocation)
    return credential_id, manager, provider, revocation


def test_credential_is_usable_before_any_revocation(setup):
    credential_id, manager, provider, revocation = setup
    credentials = provider.get_credentials(credential_id, requested_by="test_suite")
    assert credentials.api_key == "k"


def test_revoke_makes_get_credentials_fail(setup):
    credential_id, manager, provider, revocation = setup
    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")

    with pytest.raises(CredentialRevokedError):
        provider.get_credentials(credential_id, requested_by="test_suite")


def test_revoke_blocks_every_subsequent_call_not_just_the_first(setup):
    credential_id, manager, provider, revocation = setup
    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")

    for _ in range(3):
        with pytest.raises(CredentialRevokedError):
            provider.get_credentials(credential_id, requested_by="test_suite")


def test_re_grant_restores_access(setup):
    credential_id, manager, provider, revocation = setup
    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")
    assert revocation.is_revoked(credential_id) is True

    revocation.re_grant(credential_id, re_granted_by="alice")

    assert revocation.is_revoked(credential_id) is False
    credentials = provider.get_credentials(credential_id, requested_by="test_suite")
    assert credentials.api_key == "k"


def test_re_grant_without_a_prior_revocation_raises(setup):
    credential_id, manager, provider, revocation = setup
    with pytest.raises(KeyError, match="no revocation record"):
        revocation.re_grant(credential_id, re_granted_by="alice")


def test_revoke_publishes_emergency_revocation_triggered(db, audit_db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_ECR_KEK2"))
    manager = KeyLifecycleManager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    event_bus = FakeEventBus()
    revocation = EmergencyCredentialRevocation(db, manager, event_bus=event_bus)

    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")

    assert len(event_bus.published) == 1
    event = event_bus.published[0]
    assert isinstance(event, EmergencyRevocationTriggered)
    assert event.credential_id == credential_id
    assert event.reason == "suspected leak"


def test_revoke_raises_for_an_unknown_credential(db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_ECR_KEK3"))
    manager = KeyLifecycleManager(db, vault)
    revocation = EmergencyCredentialRevocation(db, manager)

    with pytest.raises(KeyError):
        revocation.revoke("does-not-exist", triggered_by="alice", reason="test")


def test_a_pending_order_is_rejected_before_it_ever_reaches_order_placement(setup):
    """The spec's own framing: an order attempted during revocation is
    rejected BEFORE it ever reaches BinanceExecutionAdapter — proven
    here with a fake "place the order" call site that must never be
    invoked once get_credentials() fails."""
    credential_id, manager, provider, revocation = setup
    revocation.revoke(credential_id, triggered_by="alice", reason="suspected leak")

    order_placement_calls = []

    def submit_order_via_adapter():
        # A real call site (step 8) fetches credentials FIRST, then
        # only proceeds to place the order if that succeeds.
        credentials = provider.get_credentials(credential_id, requested_by="order_manager")
        order_placement_calls.append(credentials.api_key)  # would place the order here

    with pytest.raises(CredentialRevokedError):
        submit_order_via_adapter()

    assert order_placement_calls == []  # order placement was never reached
