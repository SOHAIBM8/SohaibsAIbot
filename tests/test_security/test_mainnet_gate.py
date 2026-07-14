"""
The single most important test in this spec (spec section 8's own
words): mainnet=True credentials must be structurally incapable of
using LocalDevKMSClient — a raise, never a warning, never a log line
that could be missed.
"""

import pytest

from core.security.credential_vault import CredentialVault
from core.security.kms_client import KMSClient, LocalDevKMSClient
from core.security.mainnet_gate import MainnetGate, MainnetGateViolationError


class _FakeRealKMSClient(KMSClient):
    """A stand-in for a real cloud-backed KMS client — anything that
    is NOT LocalDevKMSClient must be accepted for mainnet=True."""

    @property
    def kek_key_id(self) -> str:
        return "fake-real-kms-key"

    def generate_data_key(self):
        return b"\x00" * 32, b"wrapped"

    def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        return b"\x00" * 32


def test_mainnet_true_with_local_dev_kms_raises_not_warns():
    with pytest.raises(MainnetGateViolationError):
        MainnetGate.check(mainnet=True, kms_client=LocalDevKMSClient(kek_env_var="TEST_MG_KEK"))


def test_mainnet_false_with_local_dev_kms_is_permitted():
    MainnetGate.check(mainnet=False, kms_client=LocalDevKMSClient(kek_env_var="TEST_MG_KEK2"))
    # must not raise


def test_mainnet_true_with_a_real_kms_client_is_permitted():
    MainnetGate.check(mainnet=True, kms_client=_FakeRealKMSClient())
    # must not raise


def test_mainnet_false_with_a_real_kms_client_is_permitted():
    MainnetGate.check(mainnet=False, kms_client=_FakeRealKMSClient())
    # must not raise


def test_credential_vault_encrypt_refuses_mainnet_with_local_dev_kms():
    """Proves the gate is actually wired into the real encryption path,
    not just callable in isolation — the lowest layer refuses too."""
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_MG_KEK3"))

    with pytest.raises(MainnetGateViolationError):
        vault.encrypt(api_key="k", api_secret="s", mainnet=True)


def test_credential_vault_encrypt_permits_mainnet_with_a_real_kms_client():
    vault = CredentialVault(_FakeRealKMSClient())

    payload = vault.encrypt(api_key="k", api_secret="s", mainnet=True)
    assert payload.kek_key_id == "fake-real-kms-key"


def test_credential_vault_encrypt_still_defaults_to_non_mainnet():
    """mainnet defaults to False — an existing caller that never passes
    it (e.g. all of step 1's tests) keeps working unchanged."""
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_MG_KEK4"))
    payload = vault.encrypt(api_key="k", api_secret="s")  # must not raise
    api_key, api_secret = vault.decrypt(payload)
    assert (api_key, api_secret) == ("k", "s")


def test_violation_message_names_the_rejected_client_class():
    with pytest.raises(MainnetGateViolationError, match="LocalDevKMSClient"):
        MainnetGate.check(mainnet=True, kms_client=LocalDevKMSClient(kek_env_var="TEST_MG_KEK5"))
