"""
Stage 3 security events, published on the same EventBus every other
component uses (core.ingestion.event_bus's EventLike Protocol) —
matching core/execution/events.py's/core/risk/events.py's shape and
reasoning exactly: no new transport, no dependency on the ingestion
component beyond the transport-agnostic interface.

CredentialValidationFailed carries the same severity as
KillSwitchEngaged downstream (spec section 7) — that's an alerting-
pipeline concern outside this codebase's scope, so it's not encoded in
the dataclass itself, only called out here so nobody wires the two
events to different alert channels later without noticing this note.
"""

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class SecurityEvent:
    event_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CredentialDecrypted(SecurityEvent):
    credential_id: str = ""
    requested_by: str = ""
    client_order_id: str | None = None
    occurred_at: datetime | None = None
    event_type: str = "CredentialDecrypted"


@dataclass(frozen=True)
class CredentialValidationFailed(SecurityEvent):
    credential_id: str = ""
    reason: str = ""
    occurred_at: datetime | None = None
    event_type: str = "CredentialValidationFailed"


@dataclass(frozen=True)
class ArmingStateChanged(SecurityEvent):
    account_id: str = ""
    strategy_id: str = ""
    exchange: str = ""
    armed: bool = False
    changed_by: str = ""
    occurred_at: datetime | None = None
    event_type: str = "ArmingStateChanged"


@dataclass(frozen=True)
class ArmingExpired(SecurityEvent):
    account_id: str = ""
    strategy_id: str = ""
    exchange: str = ""
    occurred_at: datetime | None = None
    event_type: str = "ArmingExpired"


@dataclass(frozen=True)
class EmergencyRevocationTriggered(SecurityEvent):
    credential_id: str = ""
    triggered_by: str = ""
    reason: str = ""
    occurred_at: datetime | None = None
    event_type: str = "EmergencyRevocationTriggered"


@dataclass(frozen=True)
class KeyRotationDue(SecurityEvent):
    credential_id: str = ""
    occurred_at: datetime | None = None
    event_type: str = "KeyRotationDue"
