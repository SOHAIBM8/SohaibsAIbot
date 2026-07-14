"""
Envelope encryption (decision #1): CredentialVault generates a fresh
per-credential DEK via the KMSClient, encrypts the API key/secret with
that DEK locally, and returns only ciphertext — the plaintext DEK never
leaves this method's stack frame. Nothing in this file persists
anything to Postgres yet (that's step 3/4's job: `encrypted_credentials`
table + `KeyLifecycleManager`/`CredentialProvider`); this step is the
encrypt/decrypt round trip in isolation, proven correct before anything
depends on it.

Design note (rule 9 — updated in step 2): `encrypt()` now takes a
`mainnet` flag and checks it via `MainnetGate` as its very first
action, before any key material is generated. This is deliberately the
LOWEST layer that could plausibly perform this check — even if a
higher-level caller (KeyLifecycleManager, step 3) somehow forgot to
gate a mainnet request, encryption itself still refuses. Belt-and-
suspenders is the right posture for decision #6, not redundant.
"""

from dataclasses import dataclass

from core.security import _aead
from core.security.kms_client import KMSClient
from core.security.mainnet_gate import MainnetGate


@dataclass
class EncryptedPayload:
    encrypted_api_key: bytes
    encrypted_api_secret: bytes
    wrapped_dek: bytes
    kek_key_id: str


class CredentialVault:
    def __init__(self, kms_client: KMSClient):
        self.kms_client = kms_client

    def encrypt(self, api_key: str, api_secret: str, mainnet: bool = False) -> EncryptedPayload:
        MainnetGate.check(mainnet, self.kms_client)
        plaintext_dek, wrapped_dek = self.kms_client.generate_data_key()
        try:
            return EncryptedPayload(
                encrypted_api_key=_aead.encrypt(plaintext_dek, api_key.encode()),
                encrypted_api_secret=_aead.encrypt(plaintext_dek, api_secret.encode()),
                wrapped_dek=wrapped_dek,
                kek_key_id=self.kms_client.kek_key_id,
            )
        finally:
            # Best-effort: Python strings/bytes are immutable and this
            # doesn't guarantee the plaintext DEK is scrubbed from
            # memory (no guaranteed zeroing in CPython) — but the
            # reference is dropped as early as physically possible
            # rather than lingering as a local for the rest of the
            # method or the caller's scope.
            del plaintext_dek

    def decrypt(self, payload: EncryptedPayload) -> tuple[str, str]:
        plaintext_dek = self.kms_client.decrypt_data_key(payload.wrapped_dek)
        try:
            api_key = _aead.decrypt(plaintext_dek, payload.encrypted_api_key).decode()
            api_secret = _aead.decrypt(plaintext_dek, payload.encrypted_api_secret).decode()
            return api_key, api_secret
        finally:
            del plaintext_dek
