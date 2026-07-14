"""
Pydantic schemas for the Settings API. CredentialSummaryOut is a
DELIBERATE subset of core.security.key_lifecycle_manager.EncryptedCredential
— it must never include encrypted_api_key/encrypted_api_secret/wrapped_dek
(spec decision #5: "the frontend never receives decrypted credential
material" — and ciphertext bytes have no business leaving the backend
either, even though they aren't plaintext). See
api/routes/settings.py's module docstring for why true "last 4
characters" masking (spec section 17's literal wording) isn't
implemented — no supporting column exists anywhere in this schema.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CredentialCreateIn(BaseModel):
    """api_key/api_secret never appear in any response body, log line,
    or CredentialSummaryOut — they exist only long enough to reach
    CredentialVault.encrypt() (see api/routes/settings.py)."""

    exchange: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    mainnet: bool = False


class CredentialSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    credential_id: str
    exchange: str
    mainnet: bool
    state: str
    created_at: datetime
    last_validated_at: datetime | None
    last_rotated_at: datetime | None
    rotation_due_at: datetime | None


class NotificationPreferencesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    email_enabled: bool
    email_address: str | None
    webhook_enabled: bool
    webhook_url: str | None
    notify_on_kill_switch: bool
    notify_on_credential_validation_failed: bool
    notify_on_drawdown_breach: bool
    updated_at: datetime | None


class NotificationPreferencesIn(BaseModel):
    email_enabled: bool = False
    email_address: str | None = None
    webhook_enabled: bool = False
    webhook_url: str | None = None
    notify_on_kill_switch: bool = True
    notify_on_credential_validation_failed: bool = True
    notify_on_drawdown_breach: bool = True
