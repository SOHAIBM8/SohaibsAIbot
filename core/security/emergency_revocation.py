"""
Decision #5: EmergencyCredentialRevocation is a distinct, MORE SEVERE
action than the kill switch — its own dedicated gate
(credential_revocation table), not a reuse of KeyLifecycleManager's
already-terminal CredentialState.REVOKED value. re_grant() is explicit
and always logged, never automatic or time-based — mirrors
core.risk.kill_switch.KillSwitch's "never auto-clears" posture, the
same design already trusted elsewhere in this codebase for exactly
this kind of panic-button semantics.

Design note (rule 9) on "invalidates cached decrypted material"
(spec section 4): nothing in this codebase caches decrypted plaintext
beyond a single call — CredentialProvider (step 4) was deliberately
built to never hold a decrypted value past get_credentials() returning.
There is therefore no separate cache to explicitly clear; the
guarantee is achieved by refusing every FUTURE decrypt instead, which
is the equivalent effect. CredentialProvider is extended in this step
with an optional `revocation` dependency it checks BEFORE ever calling
the vault — a revoked credential is never even attempted, not merely
returned-then-discarded.
"""

from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.ingestion.event_bus import EventBus
from core.security.events import EmergencyRevocationTriggered
from core.security.key_lifecycle_manager import KeyLifecycleManager

logger = structlog.get_logger(__name__)


class CredentialRevokedError(RuntimeError):
    """Raised by CredentialProvider.get_credentials() when the
    requested credential is emergency-revoked — decryption is refused
    outright, never attempted, until an explicit re_grant()."""


class EmergencyCredentialRevocation:
    def __init__(
        self,
        db: Session,
        key_lifecycle_manager: KeyLifecycleManager,
        event_bus: EventBus | None = None,
    ):
        self.db = db
        self.key_lifecycle_manager = key_lifecycle_manager
        self.event_bus = event_bus

    def revoke(self, credential_id: str, triggered_by: str, reason: str) -> None:
        self.key_lifecycle_manager.get(credential_id)  # raises KeyError if unknown
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                INSERT INTO credential_revocation
                    (credential_id, revoked, revoked_at, revoked_by, reason)
                VALUES (:credential_id, TRUE, :now, :triggered_by, :reason)
                ON CONFLICT (credential_id) DO UPDATE SET
                    revoked = TRUE,
                    revoked_at = :now,
                    revoked_by = :triggered_by,
                    reason = :reason,
                    re_granted_at = NULL,
                    re_granted_by = NULL
                """),
            {
                "credential_id": credential_id,
                "now": now,
                "triggered_by": triggered_by,
                "reason": reason,
            },
        )
        self.db.commit()
        logger.critical(
            "emergency_credential_revocation_triggered",
            credential_id=credential_id,
            triggered_by=triggered_by,
            reason=reason,
        )
        if self.event_bus is not None:
            self.event_bus.publish(
                EmergencyRevocationTriggered(
                    credential_id=credential_id,
                    triggered_by=triggered_by,
                    reason=reason,
                    occurred_at=now,
                )
            )

    def re_grant(self, credential_id: str, re_granted_by: str) -> None:
        """The explicit re-grant decision #5 requires — deliberate,
        logged, never automatic or time-based."""
        now = datetime.now(UTC)
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                    UPDATE credential_revocation
                    SET revoked = FALSE, re_granted_at = :now, re_granted_by = :re_granted_by
                    WHERE credential_id = :credential_id
                    """),
                {"credential_id": credential_id, "now": now, "re_granted_by": re_granted_by},
            ),
        )
        self.db.commit()
        if result.rowcount == 0:
            raise KeyError(f"no revocation record for credential_id={credential_id} to re-grant")
        logger.warning(
            "credential_re_granted", credential_id=credential_id, re_granted_by=re_granted_by
        )

    def is_revoked(self, credential_id: str) -> bool:
        row = (
            self.db.execute(
                text(
                    "SELECT revoked FROM credential_revocation WHERE credential_id = :credential_id"
                ),
                {"credential_id": credential_id},
            )
            .mappings()
            .first()
        )
        return bool(row["revoked"]) if row is not None else False
