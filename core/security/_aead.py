"""
Shared AES-256-GCM helper, used by both LocalDevKMSClient (to wrap/
unwrap a DEK under the local "KEK") and CredentialVault (to encrypt/
decrypt the actual credential bytes under a DEK). One small, reviewed
implementation instead of two — hand-rolling crypto twice is strictly
worse than hand-rolling it once, and this project hand-rolls it zero
times: `cryptography`'s `AESGCM` does the actual encryption/
authentication, this module only handles key generation and the
nonce||ciphertext framing so callers never have to think about nonces.

AES-GCM is authenticated encryption — decrypt() raises
`cryptography.exceptions.InvalidTag` on any tampering or wrong key,
never silently returns corrupted plaintext.
"""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LENGTH_BYTES = 12  # AES-GCM's standard/recommended nonce size


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return nonce + ciphertext


def decrypt(key: bytes, blob: bytes) -> bytes:
    nonce, ciphertext = blob[:_NONCE_LENGTH_BYTES], blob[_NONCE_LENGTH_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data=None)
