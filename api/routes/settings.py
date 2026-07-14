"""
Settings API (spec section 17/26) — credentials, notification
preferences. Two scope decisions made explicit here rather than
silently guessed:

1. "Add new credential" (spec section 17) is a mutating, security-
   consequential action that calls CredentialVault.encrypt() — the same
   category as kill-switch engage/disengage and arm/disarm. It ships
   in the control-surface step (spec decision #2), alongside those,
   via POST /credentials below. api_key/api_secret are never logged,
   never echoed in the response, and never stored anywhere but
   encrypted (matching Stage 3's own "no plaintext credential value in
   a log line, anywhere" absolute, applied identically here). Only
   testnet/paper (mainnet=False) registration is permitted — same
   reasoning as POST /api/risk/arming/arm rejecting mainnet: no real
   cloud KMS is configured in this deployment, and MainnetGate already
   forbids pairing a mainnet credential with LocalDevKMSClient anywhere
   in this system. Note (pre-existing project characteristic, not
   introduced here): LocalDevKMSClient() with no LOCAL_DEV_KEK env var
   generates a fresh ephemeral key per process — set that env var for
   a credential encrypted now to remain decryptable by a later process
   (e.g. BinanceExecutionAdapter). See kms_client.py's own docstring.

2. True "last 4 characters" credential masking (spec section 17's
   literal wording) is NOT implemented. No column anywhere in
   schema.sql stores a plaintext-adjacent fingerprint/suffix — the only
   place the actual key characters exist is inside `encrypted_api_key`
   ciphertext, and decrypting it here to compute a suffix would violate
   the same "frontend never receives decrypted credential material"
   principle the masking was supposed to serve in the first place.
   CredentialSummaryOut exposes credential_id/exchange/mainnet/state/
   timestamps only — enough for a real status badge, with zero
   plaintext or ciphertext ever leaving the backend. Adding a real
   fingerprint column (captured once at registration time, before
   plaintext leaves memory) is a schema/KeyLifecycleManager.register()
   change, not something this read-only API step can retrofit; flagged
   for CLAUDE.md's known-limitations section.

Risk config viewing reuses the already-built GET /api/risk/config
(Step 4) rather than duplicating it under /api/settings — same
"exposure, not reimplementation, once per real endpoint" reasoning as
Step 6's Portfolio page reusing GET /api/risk/decisions.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.settings import (
    CredentialCreateIn,
    CredentialSummaryOut,
    NotificationPreferencesIn,
    NotificationPreferencesOut,
)
from core.notifications.preferences_store import (
    NotificationPreferences,
    NotificationPreferencesStore,
)
from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _key_lifecycle_manager(db: Session) -> KeyLifecycleManager:
    # LocalDevKMSClient: this dashboard build has no cloud KMS
    # configured (same confirmed-with-the-user scope limit as Stage 3's
    # own KMSClient — see CLAUDE.md). This manager is only ever used
    # here for metadata READS (list_for_account/get), never encrypt(),
    # so which KMS backend it holds doesn't matter for what this route
    # actually does — kept consistent with the rest of the codebase's
    # KMSClient construction rather than inventing a decrypt-free
    # variant.
    vault = CredentialVault(LocalDevKMSClient())
    return KeyLifecycleManager(db, vault)


@router.get("/credentials", response_model=list[CredentialSummaryOut])
def list_credentials(
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> list[CredentialSummaryOut]:
    manager = _key_lifecycle_manager(db)
    credentials = manager.list_for_account(session.account_id)
    return [
        CredentialSummaryOut(
            credential_id=c.credential_id,
            exchange=c.exchange,
            mainnet=c.mainnet,
            state=c.state.value,
            created_at=c.created_at,
            last_validated_at=c.last_validated_at,
            last_rotated_at=c.last_rotated_at,
            rotation_due_at=c.rotation_due_at,
        )
        for c in credentials
    ]


@router.post(
    "/credentials", response_model=CredentialSummaryOut, status_code=status.HTTP_201_CREATED
)
def create_credential(
    body: CredentialCreateIn,
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> CredentialSummaryOut:
    if body.mainnet:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "mainnet credential registration is not permitted from this dashboard build — "
                "no real cloud KMS is configured (see CLAUDE.md's confirmed scope limit); "
                "MainnetGate forbids pairing a mainnet credential with the dev-only KMS this "
                "project uses."
            ),
        )
    manager = _key_lifecycle_manager(db)
    credential_id = manager.register(
        account_id=session.account_id,
        exchange=body.exchange,
        api_key=body.api_key,
        api_secret=body.api_secret,
        mainnet=False,
    )
    credential = manager.get(credential_id)
    return CredentialSummaryOut(
        credential_id=credential.credential_id,
        exchange=credential.exchange,
        mainnet=credential.mainnet,
        state=credential.state.value,
        created_at=credential.created_at,
        last_validated_at=credential.last_validated_at,
        last_rotated_at=credential.last_rotated_at,
        rotation_due_at=credential.rotation_due_at,
    )


@router.get("/notifications", response_model=NotificationPreferencesOut)
def get_notification_preferences(
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> NotificationPreferencesOut:
    prefs = NotificationPreferencesStore(db).get(session.account_id)
    return NotificationPreferencesOut.model_validate(prefs)


@router.put("/notifications", response_model=NotificationPreferencesOut)
def update_notification_preferences(
    body: NotificationPreferencesIn,
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> NotificationPreferencesOut:
    store = NotificationPreferencesStore(db)
    updated = store.upsert(
        NotificationPreferences(account_id=session.account_id, **body.model_dump())
    )
    return NotificationPreferencesOut.model_validate(updated)
