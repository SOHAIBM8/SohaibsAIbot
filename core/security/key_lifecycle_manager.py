"""
Credential state machine (spec section 4/5) + rotation-due reminders.
State-machine-only in this step — no exchange calls, no permission
validation logic (that's PermissionValidator, step 5). Mirrors
core/execution/order.py's shape deliberately: an explicit
_LEGAL_TRANSITIONS table, one choke-point transition method, illegal
transitions raise rather than silently clamping — a credential's
lifecycle state is exactly the kind of thing that must never be
allowed to drift into an impossible combination unnoticed.

State machine (not given verbatim by the spec — designed here,
flagged per rule 9):
- PENDING_VALIDATION -> ACTIVE: first validation passes
- PENDING_VALIDATION -> VALIDATION_FAILED: first validation fails
- ACTIVE -> VALIDATION_FAILED: a recurring re-check fails
  (decision #2 — validation is not a one-time event)
- ACTIVE -> ROTATION_DUE: the rotation reminder cadence elapses
- ROTATION_DUE -> ACTIVE: rotated and re-validated
- ROTATION_DUE -> VALIDATION_FAILED: still overdue AND a re-check fails
- VALIDATION_FAILED -> PENDING_VALIDATION: an operator fixed the
  underlying permission issue and wants to retry
- * -> REVOKED: revocation (EmergencyCredentialRevocation, step 7, or a
  routine decommission) is reachable from ANY state, including another
  REVOKED (idempotent no-op) — a credential must always be revocable,
  never "stuck" in a state that can't reach REVOKED
- REVOKED -> anything: terminal, nothing transitions out
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import structlog
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from core.ingestion.event_bus import EventBus
from core.security.credential_vault import CredentialVault

logger = structlog.get_logger(__name__)

# Confirmed with the user (spec open decision #3): 90-day rotation cadence.
DEFAULT_ROTATION_INTERVAL = timedelta(days=90)


class CredentialState(Enum):
    PENDING_VALIDATION = "pending_validation"
    ACTIVE = "active"
    VALIDATION_FAILED = "validation_failed"
    ROTATION_DUE = "rotation_due"
    REVOKED = "revoked"


_LEGAL_TRANSITIONS: dict[CredentialState, frozenset[CredentialState]] = {
    CredentialState.PENDING_VALIDATION: frozenset(
        {CredentialState.ACTIVE, CredentialState.VALIDATION_FAILED, CredentialState.REVOKED}
    ),
    CredentialState.ACTIVE: frozenset(
        {
            CredentialState.VALIDATION_FAILED,
            CredentialState.ROTATION_DUE,
            CredentialState.REVOKED,
        }
    ),
    CredentialState.VALIDATION_FAILED: frozenset(
        {CredentialState.PENDING_VALIDATION, CredentialState.REVOKED}
    ),
    CredentialState.ROTATION_DUE: frozenset(
        {
            CredentialState.ACTIVE,
            CredentialState.VALIDATION_FAILED,
            CredentialState.REVOKED,
        }
    ),
    CredentialState.REVOKED: frozenset({CredentialState.REVOKED}),  # idempotent no-op only
}


def is_legal_transition(from_state: CredentialState, to_state: CredentialState) -> bool:
    return to_state in _LEGAL_TRANSITIONS[from_state]


@dataclass
class EncryptedCredential:
    credential_id: str
    account_id: str
    exchange: str
    encrypted_api_key: bytes
    encrypted_api_secret: bytes
    wrapped_dek: bytes
    kek_key_id: str
    state: CredentialState
    mainnet: bool
    created_at: datetime
    last_validated_at: datetime | None
    last_rotated_at: datetime | None
    rotation_due_at: datetime | None


class KeyLifecycleManager:
    def __init__(
        self,
        db: Session,
        vault: CredentialVault,
        event_bus: EventBus | None = None,
        rotation_interval: timedelta = DEFAULT_ROTATION_INTERVAL,
    ):
        self.db = db
        self.vault = vault
        self.event_bus = event_bus
        self.rotation_interval = rotation_interval

    def register(
        self, account_id: str, exchange: str, api_key: str, api_secret: str, mainnet: bool
    ) -> str:
        """Encrypts and stores a new credential in PENDING_VALIDATION —
        registering a credential never implies it's trusted yet;
        PermissionValidator (step 5) is what earns it ACTIVE."""
        payload = self.vault.encrypt(api_key, api_secret, mainnet=mainnet)
        credential_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                INSERT INTO encrypted_credentials
                    (credential_id, account_id, exchange, encrypted_api_key,
                     encrypted_api_secret, wrapped_dek, kek_key_id, state, mainnet,
                     created_at, rotation_due_at)
                VALUES
                    (:credential_id, :account_id, :exchange, :encrypted_api_key,
                     :encrypted_api_secret, :wrapped_dek, :kek_key_id, :state, :mainnet,
                     :created_at, :rotation_due_at)
                """),
            {
                "credential_id": credential_id,
                "account_id": account_id,
                "exchange": exchange,
                "encrypted_api_key": payload.encrypted_api_key,
                "encrypted_api_secret": payload.encrypted_api_secret,
                "wrapped_dek": payload.wrapped_dek,
                "kek_key_id": payload.kek_key_id,
                "state": CredentialState.PENDING_VALIDATION.value,
                "mainnet": mainnet,
                "created_at": now,
                "rotation_due_at": now + self.rotation_interval,
            },
        )
        self.db.commit()
        logger.info(
            "credential_registered",
            credential_id=credential_id,
            account_id=account_id,
            exchange=exchange,
            mainnet=mainnet,
        )
        return credential_id

    def get(self, credential_id: str) -> EncryptedCredential:
        row = (
            self.db.execute(
                text("""
                    SELECT credential_id, account_id, exchange, encrypted_api_key,
                           encrypted_api_secret, wrapped_dek, kek_key_id, state, mainnet,
                           created_at, last_validated_at, last_rotated_at, rotation_due_at
                    FROM encrypted_credentials WHERE credential_id = :credential_id
                    """),
                {"credential_id": credential_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"no encrypted_credentials row with credential_id={credential_id}")
        return self._row_to_credential(row)

    def list_for_account(self, account_id: str) -> list[EncryptedCredential]:
        """Added for the dashboard's Settings page (docs/dashboard_ui_spec.md
        section 17) — no prior caller needed to list every credential
        for an account at once, only look one up by id."""
        rows = (
            self.db.execute(
                text("""
                    SELECT credential_id, account_id, exchange, encrypted_api_key,
                           encrypted_api_secret, wrapped_dek, kek_key_id, state, mainnet,
                           created_at, last_validated_at, last_rotated_at, rotation_due_at
                    FROM encrypted_credentials WHERE account_id = :account_id
                    ORDER BY created_at DESC
                    """),
                {"account_id": account_id},
            )
            .mappings()
            .all()
        )
        return [self._row_to_credential(row) for row in rows]

    @staticmethod
    def _row_to_credential(row: RowMapping) -> EncryptedCredential:
        return EncryptedCredential(
            credential_id=row["credential_id"],
            account_id=row["account_id"],
            exchange=row["exchange"],
            encrypted_api_key=bytes(row["encrypted_api_key"]),
            encrypted_api_secret=bytes(row["encrypted_api_secret"]),
            wrapped_dek=bytes(row["wrapped_dek"]),
            kek_key_id=row["kek_key_id"],
            state=CredentialState(row["state"]),
            mainnet=row["mainnet"],
            created_at=row["created_at"],
            last_validated_at=row["last_validated_at"],
            last_rotated_at=row["last_rotated_at"],
            rotation_due_at=row["rotation_due_at"],
        )

    def transition(self, credential_id: str, new_state: CredentialState) -> None:
        credential = self.get(credential_id)
        if not is_legal_transition(credential.state, new_state):
            raise ValueError(
                f"illegal credential state transition: {credential.state.value} -> "
                f"{new_state.value} (credential_id={credential_id})"
            )
        now = datetime.now(UTC)
        extra_column = ""
        params: dict = {"credential_id": credential_id, "state": new_state.value, "now": now}
        if new_state == CredentialState.ACTIVE:
            extra_column = ", last_validated_at = :now"
        self.db.execute(
            text(f"""
                UPDATE encrypted_credentials
                SET state = :state{extra_column}
                WHERE credential_id = :credential_id
                """),
            params,
        )
        self.db.commit()
        logger.info(
            "credential_state_transitioned",
            credential_id=credential_id,
            from_state=credential.state.value,
            to_state=new_state.value,
        )

    def record_validation_success(self, credential_id: str) -> None:
        """Added in step 5 for decision #2 ('a stale one-time check is
        not acceptable'): a successful RE-check on an already-ACTIVE
        credential must update last_validated_at so a recurring pass is
        actually provable — but ACTIVE -> ACTIVE isn't a real state
        transition, so it can't go through transition()'s legality
        table (which would (correctly) reject it as a same-state
        no-op). A credential still in PENDING_VALIDATION/ROTATION_DUE
        DOES have a real transition to make, so this delegates to
        transition() for those two cases instead of duplicating that
        path."""
        credential = self.get(credential_id)
        if credential.state in (CredentialState.PENDING_VALIDATION, CredentialState.ROTATION_DUE):
            self.transition(credential_id, CredentialState.ACTIVE)
            return
        if credential.state != CredentialState.ACTIVE:
            raise ValueError(
                "cannot record a validation success for a credential in state "
                f"{credential.state.value} (credential_id={credential_id}) — only "
                "PENDING_VALIDATION/ROTATION_DUE/ACTIVE credentials can be validated"
            )
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                UPDATE encrypted_credentials SET last_validated_at = :now
                WHERE credential_id = :credential_id
                """),
            {"now": now, "credential_id": credential_id},
        )
        self.db.commit()
        logger.info("credential_validation_recorded", credential_id=credential_id)

    def list_credential_ids_by_state(self, state: CredentialState) -> list[str]:
        rows = (
            self.db.execute(
                text("SELECT credential_id FROM encrypted_credentials WHERE state = :state"),
                {"state": state.value},
            )
            .mappings()
            .all()
        )
        return [row["credential_id"] for row in rows]

    def sweep_rotation_due(self, now: datetime | None = None) -> list[str]:
        """ACTIVE credentials whose rotation_due_at has passed move to
        ROTATION_DUE and get a KeyRotationDue event — a reminder, not
        an automatic key change (rotating a credential means generating
        a NEW api key/secret pair on the exchange, which nothing in
        this codebase can do on the user's behalf)."""
        now = now or datetime.now(UTC)
        rows = (
            self.db.execute(
                text("""
                SELECT credential_id FROM encrypted_credentials
                WHERE state = :active AND rotation_due_at <= :now
                """),
                {"active": CredentialState.ACTIVE.value, "now": now},
            )
            .mappings()
            .all()
        )

        due_ids = [row["credential_id"] for row in rows]
        for credential_id in due_ids:
            self.transition(credential_id, CredentialState.ROTATION_DUE)
            if self.event_bus is not None:
                from core.security.events import KeyRotationDue

                self.event_bus.publish(KeyRotationDue(credential_id=credential_id, occurred_at=now))
        return due_ids
