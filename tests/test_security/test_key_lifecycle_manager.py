"""
Tests run against real local Postgres, not mocks — consistent with
every other DB-touching component in this project. State-machine-only
scope (step 3): no exchange calls, no validation logic.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.security.credential_vault import CredentialVault, EncryptedPayload
from core.security.events import KeyRotationDue
from core.security.key_lifecycle_manager import (
    CredentialState,
    KeyLifecycleManager,
    is_legal_transition,
)
from core.security.kms_client import LocalDevKMSClient

ACCOUNT_ID = "test_klm_account"


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
            text("DELETE FROM encrypted_credentials WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
        session.commit()
        session.close()


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault(LocalDevKMSClient(kek_env_var="TEST_KLM_KEK"))


def make_manager(
    db, vault, event_bus=None, rotation_interval=timedelta(days=90)
) -> KeyLifecycleManager:
    return KeyLifecycleManager(db, vault, event_bus=event_bus, rotation_interval=rotation_interval)


def test_register_stores_only_ciphertext_in_pending_validation(db, vault):
    manager = make_manager(db, vault)

    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )

    credential = manager.get(credential_id)
    assert credential.state == CredentialState.PENDING_VALIDATION
    assert credential.account_id == ACCOUNT_ID
    assert credential.mainnet is False
    assert b"k" not in credential.encrypted_api_key or len(credential.encrypted_api_key) > 1
    # round trip via the vault confirms it's genuinely decryptable
    api_key, api_secret = vault.decrypt(
        EncryptedPayload(
            encrypted_api_key=credential.encrypted_api_key,
            encrypted_api_secret=credential.encrypted_api_secret,
            wrapped_dek=credential.wrapped_dek,
            kek_key_id=credential.kek_key_id,
        )
    )
    assert (api_key, api_secret) == ("k", "s")


def test_register_sets_rotation_due_at_using_the_configured_interval(db, vault):
    manager = make_manager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    credential = manager.get(credential_id)
    delta = credential.rotation_due_at - credential.created_at
    assert timedelta(days=89) < delta < timedelta(days=91)


def test_get_raises_for_unknown_credential(db, vault):
    manager = make_manager(db, vault)
    with pytest.raises(KeyError, match="no encrypted_credentials row"):
        manager.get("does-not-exist")


@pytest.mark.parametrize(
    "from_state,to_state,legal",
    [
        (CredentialState.PENDING_VALIDATION, CredentialState.ACTIVE, True),
        (CredentialState.PENDING_VALIDATION, CredentialState.VALIDATION_FAILED, True),
        (CredentialState.PENDING_VALIDATION, CredentialState.REVOKED, True),
        (CredentialState.PENDING_VALIDATION, CredentialState.ROTATION_DUE, False),
        (CredentialState.ACTIVE, CredentialState.VALIDATION_FAILED, True),
        (CredentialState.ACTIVE, CredentialState.ROTATION_DUE, True),
        (CredentialState.ACTIVE, CredentialState.REVOKED, True),
        (CredentialState.ACTIVE, CredentialState.PENDING_VALIDATION, False),
        (CredentialState.VALIDATION_FAILED, CredentialState.PENDING_VALIDATION, True),
        (CredentialState.VALIDATION_FAILED, CredentialState.REVOKED, True),
        (CredentialState.VALIDATION_FAILED, CredentialState.ACTIVE, False),
        (CredentialState.ROTATION_DUE, CredentialState.ACTIVE, True),
        (CredentialState.ROTATION_DUE, CredentialState.VALIDATION_FAILED, True),
        (CredentialState.ROTATION_DUE, CredentialState.REVOKED, True),
        (CredentialState.ROTATION_DUE, CredentialState.PENDING_VALIDATION, False),
        (CredentialState.REVOKED, CredentialState.REVOKED, True),
        (CredentialState.REVOKED, CredentialState.ACTIVE, False),
        (CredentialState.REVOKED, CredentialState.PENDING_VALIDATION, False),
    ],
)
def test_transition_legality_matrix(from_state, to_state, legal):
    assert is_legal_transition(from_state, to_state) is legal


def test_transition_updates_state_and_persists(db, vault):
    manager = make_manager(db, vault)
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )

    manager.transition(credential_id, CredentialState.ACTIVE)

    credential = manager.get(credential_id)
    assert credential.state == CredentialState.ACTIVE
    assert credential.last_validated_at is not None


def test_illegal_transition_raises_and_leaves_state_unchanged(db, vault):
    manager = make_manager(db, vault)
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )

    with pytest.raises(ValueError, match="illegal credential state transition"):
        manager.transition(credential_id, CredentialState.ROTATION_DUE)

    assert manager.get(credential_id).state == CredentialState.PENDING_VALIDATION


def test_revoked_is_terminal_and_idempotent(db, vault):
    manager = make_manager(db, vault)
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.REVOKED)

    manager.transition(credential_id, CredentialState.REVOKED)  # idempotent, must not raise

    with pytest.raises(ValueError):
        manager.transition(credential_id, CredentialState.ACTIVE)


def test_sweep_rotation_due_transitions_only_active_credentials_past_due(db, vault):
    manager = make_manager(db, vault, rotation_interval=timedelta(days=90))
    due_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k1", api_secret="s1", mainnet=False
    )
    not_due_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k2", api_secret="s2", mainnet=False
    )
    manager.transition(due_id, CredentialState.ACTIVE)
    manager.transition(not_due_id, CredentialState.ACTIVE)
    # force due_id's rotation_due_at into the past
    db.execute(
        text("UPDATE encrypted_credentials SET rotation_due_at = :t WHERE credential_id = :c"),
        {"t": datetime.now(UTC) - timedelta(days=1), "c": due_id},
    )
    db.commit()

    swept = manager.sweep_rotation_due()

    assert swept == [due_id]
    assert manager.get(due_id).state == CredentialState.ROTATION_DUE
    assert manager.get(not_due_id).state == CredentialState.ACTIVE


def test_sweep_rotation_due_publishes_key_rotation_due(db, vault):
    event_bus = FakeEventBus()
    manager = make_manager(db, vault, event_bus=event_bus, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.ACTIVE)
    db.execute(
        text("UPDATE encrypted_credentials SET rotation_due_at = :t WHERE credential_id = :c"),
        {"t": datetime.now(UTC) - timedelta(days=1), "c": credential_id},
    )
    db.commit()

    manager.sweep_rotation_due()

    assert len(event_bus.published) == 1
    assert isinstance(event_bus.published[0], KeyRotationDue)
    assert event_bus.published[0].credential_id == credential_id


def test_sweep_rotation_due_ignores_credentials_not_yet_due(db, vault):
    manager = make_manager(db, vault, rotation_interval=timedelta(days=90))
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    manager.transition(credential_id, CredentialState.ACTIVE)

    swept = manager.sweep_rotation_due()

    assert swept == []
    assert manager.get(credential_id).state == CredentialState.ACTIVE
