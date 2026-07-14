"""
Decision #2: withdrawal-permission validation runs at connection time
AND on a recurring schedule — validate() is the single method both
call sites use; sweep_active_credentials() is what the Scheduler
triggers repeatedly (step-8-equivalent "connection time" call sites
call validate() directly, once, before trusting a credential).

Any withdrawal-enabled finding is treated as compromised-looking, not
merely noncompliant — decision #2's whole point is that a trading-only
credential should never be able to withdraw funds, so a key that CAN
is either misconfigured or a signal something is wrong. It:
1. transitions the credential to VALIDATION_FAILED,
2. disarms trading for every (account, strategy) using it, via an
   injected Disarmer — a structural Protocol, not a hard dependency on
   ArmingService (step 6), so this file doesn't need that class to
   exist yet to be complete and testable on its own,
3. publishes CredentialValidationFailed, same alerting severity as
   KillSwitchEngaged (spec section 7).

All three happen every time, in that order, for the "full chain, not
just the classification" the spec's testing strategy asks for.
"""

from datetime import UTC, datetime
from typing import Protocol

import structlog

from core.security.credential_provider import CredentialProvider
from core.security.events import CredentialValidationFailed
from core.security.key_lifecycle_manager import CredentialState, KeyLifecycleManager
from core.security.permission_checker import ExchangePermissionChecker, PermissionCheckResult

logger = structlog.get_logger(__name__)


class Disarmer(Protocol):
    """Satisfied by ArmingService (step 6) — kept as a structural
    Protocol here so PermissionValidator has no import-time dependency
    on a class this step doesn't build."""

    def disarm_all(self, account_id: str, exchange: str, reason: str) -> None: ...


class PermissionValidator:
    def __init__(
        self,
        key_lifecycle_manager: KeyLifecycleManager,
        credential_provider: CredentialProvider,
        permission_checker: ExchangePermissionChecker,
        event_bus: object | None = None,
        disarmer: Disarmer | None = None,
    ):
        self.key_lifecycle_manager = key_lifecycle_manager
        self.credential_provider = credential_provider
        self.permission_checker = permission_checker
        self.event_bus = event_bus
        self.disarmer = disarmer

    def validate(
        self, credential_id: str, requested_by: str = "permission_validator"
    ) -> PermissionCheckResult:
        credential = self.key_lifecycle_manager.get(credential_id)
        live_credentials = self.credential_provider.get_credentials(
            credential_id, requested_by=requested_by
        )
        result = self.permission_checker.check_permissions(
            live_credentials.api_key, live_credentials.api_secret
        )

        if result.withdrawals_enabled:
            self._fail(credential_id, credential.account_id, credential.exchange)
        else:
            self.key_lifecycle_manager.record_validation_success(credential_id)

        return result

    def _fail(self, credential_id: str, account_id: str, exchange: str) -> None:
        reason = "withdrawal_permission_enabled"
        self.key_lifecycle_manager.transition(credential_id, CredentialState.VALIDATION_FAILED)

        if self.disarmer is not None:
            self.disarmer.disarm_all(account_id, exchange, reason=reason)

        logger.error(
            "credential_validation_failed",
            credential_id=credential_id,
            account_id=account_id,
            exchange=exchange,
            reason=reason,
        )

        if self.event_bus is not None:
            self.event_bus.publish(  # type: ignore[attr-defined]
                CredentialValidationFailed(
                    credential_id=credential_id, reason=reason, occurred_at=datetime.now(UTC)
                )
            )

    def sweep_active_credentials(self) -> list[str]:
        """The recurring re-check (decision #2) — validates every
        currently-ACTIVE credential regardless of whether anything
        seemed wrong, same "run on a fixed cadence, no-op is still
        evidence" posture as ReconciliationJob (Stage 2)."""
        credential_ids = self.key_lifecycle_manager.list_credential_ids_by_state(
            CredentialState.ACTIVE
        )
        for credential_id in credential_ids:
            self.validate(credential_id, requested_by="permission_validator_sweep")
        return credential_ids
