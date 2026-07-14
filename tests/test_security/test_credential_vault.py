"""
Encryption round-trip tests, against a LocalDevKMSClient fake — never
a real cloud KMS in the unit suite (spec section 8).
"""

import pytest
from cryptography.exceptions import InvalidTag

from core.security.credential_vault import CredentialVault, EncryptedPayload
from core.security.kms_client import LocalDevKMSClient


def make_vault() -> CredentialVault:
    return CredentialVault(LocalDevKMSClient(kek_env_var="TEST_LOCAL_DEV_KEK_VAULT"))


def test_encrypt_then_decrypt_returns_the_original_values():
    vault = make_vault()

    payload = vault.encrypt(api_key="test-api-key-12345", api_secret="test-secret-67890")
    api_key, api_secret = vault.decrypt(payload)

    assert api_key == "test-api-key-12345"
    assert api_secret == "test-secret-67890"


def test_encrypted_payload_never_contains_the_plaintext():
    vault = make_vault()
    api_key = "super-secret-api-key-value"
    api_secret = "super-secret-secret-value"

    payload = vault.encrypt(api_key=api_key, api_secret=api_secret)

    assert api_key.encode() not in payload.encrypted_api_key
    assert api_secret.encode() not in payload.encrypted_api_secret
    assert api_key.encode() not in payload.wrapped_dek
    assert api_secret.encode() not in payload.wrapped_dek


def test_two_encryptions_of_the_same_value_produce_different_ciphertext():
    """A fresh DEK + fresh nonce every call — proves this isn't a
    deterministic (and therefore weaker) encryption scheme."""
    vault = make_vault()

    first = vault.encrypt(api_key="same-value", api_secret="same-secret")
    second = vault.encrypt(api_key="same-value", api_secret="same-secret")

    assert first.encrypted_api_key != second.encrypted_api_key
    assert first.wrapped_dek != second.wrapped_dek


def test_payload_records_the_kek_key_id():
    vault = make_vault()
    payload = vault.encrypt(api_key="k", api_secret="s")
    assert payload.kek_key_id == "local-dev-kek-v1"


def test_decrypt_fails_closed_on_tampered_ciphertext():
    vault = make_vault()
    payload = vault.encrypt(api_key="k", api_secret="s")

    tampered = EncryptedPayload(
        encrypted_api_key=payload.encrypted_api_key[:-1]
        + bytes([payload.encrypted_api_key[-1] ^ 1]),
        encrypted_api_secret=payload.encrypted_api_secret,
        wrapped_dek=payload.wrapped_dek,
        kek_key_id=payload.kek_key_id,
    )

    with pytest.raises(InvalidTag):
        vault.decrypt(tampered)


def test_decrypt_fails_closed_with_the_wrong_kek():
    vault = make_vault()
    payload = vault.encrypt(api_key="k", api_secret="s")

    other_vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_LOCAL_DEV_KEK_VAULT_OTHER"))
    with pytest.raises(InvalidTag):
        other_vault.decrypt(payload)


def test_kek_persists_across_vault_instances_when_env_var_is_set(monkeypatch):
    """A LocalDevKMSClient constructed with the SAME env var name reads
    the SAME KEK — proves the "generated ephemeral" fallback only
    applies when nothing is set, not that every instance is isolated."""
    import base64

    from core.security import _aead

    fixed_kek = base64.urlsafe_b64encode(_aead.generate_key()).decode()
    monkeypatch.setenv("TEST_LOCAL_DEV_KEK_FIXED", fixed_kek)

    vault_a = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_LOCAL_DEV_KEK_FIXED"))
    payload = vault_a.encrypt(api_key="k", api_secret="s")

    vault_b = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_LOCAL_DEV_KEK_FIXED"))
    api_key, api_secret = vault_b.decrypt(payload)

    assert api_key == "k"
    assert api_secret == "s"
