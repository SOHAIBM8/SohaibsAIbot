"""
The seam BinanceExecutionAdapter (Stage 2) will depend on to obtain
live credentials (decision #7 — order-placement logic itself never
changes, only where credentials come from; wired in step 8).
get_credentials() decrypts on demand and writes exactly one
credential_audit_log row BEFORE returning anything, via the dedicated
INSERT-only `credential_audit_writer` connection (core/security/audit_db.py)
— never the default app session, so the audit write itself is subject
to decision #4's enforced-at-the-role-level guarantee even from inside
this class. Never caches plaintext beyond the immediate call: no
instance attribute here ever holds a decrypted value once
get_credentials() returns.

Decision #8, absolute: no plaintext credential value may appear in a
log line, at any level, anywhere. This file's structlog calls only
ever include credential_id/account_id/exchange/requested_by/
client_order_id — never api_key/api_secret, not even truncated, not
even at debug level. test_no_plaintext_in_logs.py proves this across
the whole decrypt-and-audit path, not just by inspecting this file.

Design note (rule 9, updated in step 7): an optional `revocation`
dependency (EmergencyCredentialRevocation) is now checked FIRST, before
the vault is ever touched — a revoked credential is never even
attempted, not merely returned-then-discarded. Left optional (default
None) rather than required so step 4's own tests, written before this
class existed, keep working unchanged; every real call site should
supply one once step 8 wires this into BinanceExecutionAdapter.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.security.credential_vault import CredentialVault, EncryptedPayload
from core.security.emergency_revocation import CredentialRevokedError, EmergencyCredentialRevocation
from core.security.key_lifecycle_manager import KeyLifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class LiveCredentials:
    api_key: str
    api_secret: str


class CredentialProvider:
    def __init__(
        self,
        key_lifecycle_manager: KeyLifecycleManager,
        vault: CredentialVault,
        audit_db: Session,
        revocation: EmergencyCredentialRevocation | None = None,
    ):
        self.key_lifecycle_manager = key_lifecycle_manager
        self.vault = vault
        self.audit_db = audit_db
        self.revocation = revocation

    def get_credentials(
        self, credential_id: str, requested_by: str, client_order_id: str | None = None
    ) -> LiveCredentials:
        if self.revocation is not None and self.revocation.is_revoked(credential_id):
            logger.error(
                "credential_decrypt_refused_revoked",
                credential_id=credential_id,
                requested_by=requested_by,
            )
            raise CredentialRevokedError(
                f"credential_id={credential_id} is emergency-revoked; "
                "explicit re-grant required before it can be decrypted again"
            )

        credential = self.key_lifecycle_manager.get(credential_id)
        payload = EncryptedPayload(
            encrypted_api_key=credential.encrypted_api_key,
            encrypted_api_secret=credential.encrypted_api_secret,
            wrapped_dek=credential.wrapped_dek,
            kek_key_id=credential.kek_key_id,
        )
        api_key, api_secret = self.vault.decrypt(payload)

        # Audit write happens BEFORE returning anything to the caller
        # (spec section 3) — a caller must never be able to observe a
        # decrypted credential that isn't already durably logged.
        self._audit(credential_id, "decrypted", requested_by, client_order_id)
        logger.info(
            "credential_decrypted",
            credential_id=credential_id,
            account_id=credential.account_id,
            exchange=credential.exchange,
            requested_by=requested_by,
            client_order_id=client_order_id,
        )
        return LiveCredentials(api_key=api_key, api_secret=api_secret)

    def _audit(
        self, credential_id: str, action: str, requested_by: str, client_order_id: str | None
    ) -> None:
        self.audit_db.execute(
            text("""
                INSERT INTO credential_audit_log
                    (credential_id, action, requested_by, client_order_id, occurred_at)
                VALUES
                    (:credential_id, :action, :requested_by, :client_order_id, :occurred_at)
                """),
            {
                "credential_id": credential_id,
                "action": action,
                "requested_by": requested_by,
                "client_order_id": client_order_id,
                "occurred_at": datetime.now(UTC),
            },
        )
        self.audit_db.commit()
