"""
KMSClient is the pluggable seam for envelope encryption's outer layer
(decision #1: per-credential DEK, wrapped by a KEK held in an external
KMS, never co-located with the encrypted data). Same interface-first
pattern as every other exchange/adapter boundary in this project
(`ExchangeAdapter`, `ExecutionAdapter`) — a real cloud KMS
implementation plugs in without `CredentialVault` changing.

Design note (rule 9, confirmed with the user before step 1): this
project has no cloud infrastructure configured yet, so only
`LocalDevKMSClient` (testnet-only local development) is a real,
functional implementation here. `AWSKMSClient` is left as an
unimplemented stub — every method raises `NotImplementedError` — the
same pattern Stage 1 used for `LiveExecutionAdapter` before Stage 2
existed. Building a real AWS KMS integration against credentials
nobody has configured would be untestable scope creep, not a genuine
capability; `MainnetGate` (step 2) is what actually prevents anyone
from reaching for `LocalDevKMSClient` when `mainnet=True` in the
meantime.
"""

import base64
import os
from abc import ABC, abstractmethod

import structlog

from core.security import _aead

logger = structlog.get_logger(__name__)

_STAGE_3_AWS_KMS_MESSAGE = (
    "AWSKMSClient is an interface stub — no cloud KMS infrastructure is "
    "configured for this project yet. See docs/execution_engine_stage3_spec.md "
    "section 11, open decision #1."
)


class KMSClient(ABC):
    @property
    @abstractmethod
    def kek_key_id(self) -> str:
        """A stable identifier for the KEK in use — persisted alongside
        each encrypted credential (`encrypted_credentials.kek_key_id`)
        so a future key rotation can tell which KEK a given row was
        wrapped under."""

    @abstractmethod
    def generate_data_key(self) -> tuple[bytes, bytes]:
        """Returns (plaintext_dek, wrapped_dek) — a fresh 256-bit DEK,
        plus that same DEK encrypted ("wrapped") under the KEK. Only
        the wrapped form is ever persisted; the plaintext form exists
        only in memory for the immediate encrypt/decrypt call that
        needs it."""

    @abstractmethod
    def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        """Unwraps a previously-wrapped DEK back to its plaintext form
        using the KEK. This is the one KMS call a credential decrypt
        actually depends on — everything else (the credential
        ciphertext itself) is decrypted locally once the DEK is known."""


class LocalDevKMSClient(KMSClient):
    """Testnet-only local development stand-in. Does NOT call any real
    cloud service — the "KEK" is a locally-held symmetric key, read
    from an environment variable (generated and logged — never as its
    own value, only a warning that it happened — if absent, so local
    dev still works out of the box). This is deliberately NOT a
    production KMS and never will be: `MainnetGate` (step 2)
    structurally forbids pairing this class with `mainnet=True`
    anywhere in the system, not just by convention."""

    def __init__(self, kek_env_var: str = "LOCAL_DEV_KEK", key_id: str = "local-dev-kek-v1"):
        raw = os.environ.get(kek_env_var)
        if raw is None:
            raw = base64.urlsafe_b64encode(_aead.generate_key()).decode()
            logger.warning(
                "local_dev_kek_generated_ephemeral",
                key_id=key_id,
                note="no persisted KEK — encrypted credentials from a prior process "
                "run will not decrypt after a restart; set the env var for persistence",
            )
        self._kek = base64.urlsafe_b64decode(raw)
        self._key_id = key_id

    @property
    def kek_key_id(self) -> str:
        return self._key_id

    def generate_data_key(self) -> tuple[bytes, bytes]:
        plaintext_dek = _aead.generate_key()
        wrapped_dek = _aead.encrypt(self._kek, plaintext_dek)
        return plaintext_dek, wrapped_dek

    def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        return _aead.decrypt(self._kek, wrapped_dek)


class AWSKMSClient(KMSClient):
    """Stage 3 stub — see this module's docstring. Every method raises
    `NotImplementedError`; do not add boto3 calls here until real AWS
    KMS infrastructure exists to point at and test against."""

    def __init__(self, key_arn: str):
        self.key_arn = key_arn

    @property
    def kek_key_id(self) -> str:
        raise NotImplementedError(_STAGE_3_AWS_KMS_MESSAGE)

    def generate_data_key(self) -> tuple[bytes, bytes]:
        raise NotImplementedError(_STAGE_3_AWS_KMS_MESSAGE)

    def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        raise NotImplementedError(_STAGE_3_AWS_KMS_MESSAGE)
