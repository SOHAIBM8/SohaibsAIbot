"""
Per (account, strategy, exchange) trading consent (decision #3):
armed/disarmed, expires, requires re-confirmation after any config
change. Distinct from — and checked independently alongside, never
merged into — the Risk Engine's existing KillSwitch: `is_trading_permitted()`
at the bottom of this module is the one function that combines both,
so neither class needs to import or know about the other.

is_armed() computes expiry at READ time: a row can be `armed = TRUE`
in Postgres past its `expires_at` (nothing sweeps it automatically),
but is_armed() still correctly reports False the instant `expires_at`
passes, with no explicit disarm() call required — exactly what the
spec's expiry test asks for. disarm() is still the right call when you
want the PERSISTED row to reflect reality (e.g. for an audit/listing
view), but nothing depends on it having been called for the live
permission check to be correct.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.ingestion.event_bus import EventBus
from core.risk.kill_switch import KillSwitch
from core.security.events import ArmingStateChanged

logger = structlog.get_logger(__name__)

# Confirmed with the user (spec open decision #2): 48-hour arming expiry.
DEFAULT_ARMING_DURATION = timedelta(hours=48)


@dataclass
class ArmingState:
    account_id: str
    strategy_id: str
    exchange: str
    armed: bool
    armed_at: datetime | None
    expires_at: datetime | None
    armed_by: str | None
    mainnet: bool


class ArmingService:
    def __init__(
        self,
        db: Session,
        event_bus: EventBus | None = None,
        arming_duration: timedelta = DEFAULT_ARMING_DURATION,
    ):
        self.db = db
        self.event_bus = event_bus
        self.arming_duration = arming_duration

    def arm(
        self, account_id: str, strategy_id: str, exchange: str, armed_by: str, mainnet: bool
    ) -> None:
        now = datetime.now(UTC)
        expires_at = now + self.arming_duration
        self.db.execute(
            text("""
                INSERT INTO arming_state
                    (account_id, strategy_id, exchange, armed, armed_at, expires_at,
                     armed_by, mainnet)
                VALUES
                    (:account_id, :strategy_id, :exchange, TRUE, :now, :expires_at,
                     :armed_by, :mainnet)
                ON CONFLICT (account_id, strategy_id, exchange) DO UPDATE SET
                    armed = TRUE,
                    armed_at = :now,
                    expires_at = :expires_at,
                    armed_by = :armed_by,
                    mainnet = :mainnet
                """),
            {
                "account_id": account_id,
                "strategy_id": strategy_id,
                "exchange": exchange,
                "now": now,
                "expires_at": expires_at,
                "armed_by": armed_by,
                "mainnet": mainnet,
            },
        )
        self.db.commit()
        logger.warning(
            "arming_state_armed",
            account_id=account_id,
            strategy_id=strategy_id,
            exchange=exchange,
            armed_by=armed_by,
            mainnet=mainnet,
            expires_at=expires_at.isoformat(),
        )
        self._publish_changed(account_id, strategy_id, exchange, armed=True, changed_by=armed_by)

    def disarm(self, account_id: str, strategy_id: str, exchange: str, reason: str) -> None:
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                    UPDATE arming_state SET armed = FALSE
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND exchange = :exchange
                    """),
                {"account_id": account_id, "strategy_id": strategy_id, "exchange": exchange},
            ),
        )
        self.db.commit()
        if result.rowcount == 0:
            return  # nothing to disarm — never existed or already absent
        logger.warning(
            "arming_state_disarmed",
            account_id=account_id,
            strategy_id=strategy_id,
            exchange=exchange,
            reason=reason,
        )
        self._publish_changed(account_id, strategy_id, exchange, armed=False, changed_by=reason)

    def disarm_all(self, account_id: str, exchange: str, reason: str) -> None:
        """Satisfies PermissionValidator's Disarmer Protocol (step 5) —
        a compromised-looking credential must disarm EVERY strategy
        using its (account, exchange), since PermissionValidator has no
        way to know which specific strategies are affected."""
        rows = (
            self.db.execute(
                text("""
                SELECT strategy_id FROM arming_state
                WHERE account_id = :account_id AND exchange = :exchange AND armed = TRUE
                """),
                {"account_id": account_id, "exchange": exchange},
            )
            .mappings()
            .all()
        )
        for row in rows:
            self.disarm(account_id, row["strategy_id"], exchange, reason=reason)

    def on_config_changed(self, account_id: str, strategy_id: str, exchange: str) -> None:
        """Decision #3: any parameter change on an armed strategy
        reverts it to unarmed, requiring re-confirmation — arming is
        consent to the CURRENT configuration, not a standing consent
        that survives a change nobody re-confirmed."""
        self.disarm(account_id, strategy_id, exchange, reason="config_changed")

    def is_armed(
        self, account_id: str, strategy_id: str, exchange: str, now: datetime | None = None
    ) -> bool:
        now = now or datetime.now(UTC)
        row = (
            self.db.execute(
                text("""
                    SELECT armed, expires_at FROM arming_state
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND exchange = :exchange
                    """),
                {"account_id": account_id, "strategy_id": strategy_id, "exchange": exchange},
            )
            .mappings()
            .first()
        )
        if row is None or not row["armed"]:
            return False
        if row["expires_at"] is None or now >= row["expires_at"]:
            return False
        return True

    def get(self, account_id: str, strategy_id: str, exchange: str) -> ArmingState | None:
        row = (
            self.db.execute(
                text("""
                    SELECT account_id, strategy_id, exchange, armed, armed_at, expires_at,
                           armed_by, mainnet
                    FROM arming_state
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND exchange = :exchange
                    """),
                {"account_id": account_id, "strategy_id": strategy_id, "exchange": exchange},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return ArmingState(**row)

    def _publish_changed(
        self, account_id: str, strategy_id: str, exchange: str, armed: bool, changed_by: str
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            ArmingStateChanged(
                account_id=account_id,
                strategy_id=strategy_id,
                exchange=exchange,
                armed=armed,
                changed_by=changed_by,
                occurred_at=datetime.now(UTC),
            )
        )


def is_trading_permitted(
    kill_switch: KillSwitch,
    arming_service: ArmingService,
    account_id: str,
    strategy_id: str,
    exchange: str,
    now: datetime | None = None,
) -> bool:
    """The dual gate (spec section 3): BOTH the kill switch must be
    clear AND arming must be active — neither gate alone is
    sufficient, and neither is checked by, or bypasses, the other.
    `now` is forwarded to is_armed() so a test can simulate elapsed
    time without monkeypatching or a real sleep."""
    if kill_switch.is_engaged():
        return False
    return arming_service.is_armed(account_id, strategy_id, exchange, now=now)
