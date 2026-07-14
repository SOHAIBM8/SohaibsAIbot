"""
KillSwitch (spec decision #6): persisted to Postgres, not held only in
memory, because a process restart must never silently clear an
emergency stop. NEVER auto-clears — engage()/disengage() are both
explicit calls; nothing in this class calls disengage() on its own.
Auto-engage triggers (drawdown tier 3 breach, N circuit breaker trips)
live in DrawdownMonitor/RiskEngine, which call engage() with an
appropriate reason — KillSwitch itself only owns the engage/disengage/
persist mechanics, not the policy of when to trip.

Default scope = "block new trades only" (decision #2): KillSwitch
itself doesn't touch open positions — RiskEngine consults
is_engaged() to veto new sizing decisions, while existing positions
keep being monitored/marked-to-market/exited on their own stops
regardless. Auto-flatten (closing existing positions) is a separate,
opt-in RiskConfig flag this class doesn't implement.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


@dataclass
class KillSwitchState:
    """Full persisted row, not just the engaged bool — added for the
    dashboard's Risk monitoring page (docs/dashboard_ui_spec.md
    section 14), which needs to show who engaged it, when, and why,
    not just a boolean."""

    scope: str
    engaged: bool
    engaged_at: datetime | None
    engaged_reason: str | None
    engaged_by: str | None
    updated_at: datetime | None


class KillSwitch:
    def __init__(self, db: Session, scope: str = "global"):
        self.db = db
        self.scope = scope
        self._engaged = False
        self.load_state()

    def load_state(self) -> None:
        """Read current state from kill_switch_state — this is what
        makes persistence actually matter. Called on construction so a
        freshly-instantiated KillSwitch (e.g. after a process restart)
        reflects whatever was last persisted, not a clean-slate False."""
        row = (
            self.db.execute(
                text("SELECT engaged FROM kill_switch_state WHERE scope = :scope"),
                {"scope": self.scope},
            )
            .mappings()
            .first()
        )
        self._engaged = bool(row["engaged"]) if row is not None else False

    def is_engaged(self) -> bool:
        return self._engaged

    def get_state(self) -> KillSwitchState:
        """Full row for display — reads fresh from Postgres rather than
        the cached `_engaged` bool, so a status page always reflects
        the latest persisted state even if another process engaged/
        disengaged since this instance was constructed."""
        row = (
            self.db.execute(
                text("""
                    SELECT scope, engaged, engaged_at, engaged_reason, engaged_by, updated_at
                    FROM kill_switch_state WHERE scope = :scope
                    """),
                {"scope": self.scope},
            )
            .mappings()
            .first()
        )
        if row is None:
            return KillSwitchState(
                scope=self.scope,
                engaged=False,
                engaged_at=None,
                engaged_reason=None,
                engaged_by=None,
                updated_at=None,
            )
        return KillSwitchState(
            scope=row["scope"],
            engaged=bool(row["engaged"]),
            engaged_at=row["engaged_at"],
            engaged_reason=row["engaged_reason"],
            engaged_by=row["engaged_by"],
            updated_at=row["updated_at"],
        )

    def engage(self, reason: str, engaged_by: str) -> None:
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                INSERT INTO kill_switch_state
                    (scope, engaged, engaged_at, engaged_reason, engaged_by, updated_at)
                VALUES (:scope, TRUE, :now, :reason, :engaged_by, :now)
                ON CONFLICT (scope) DO UPDATE SET
                    engaged = TRUE,
                    engaged_at = :now,
                    engaged_reason = :reason,
                    engaged_by = :engaged_by,
                    updated_at = :now
                """),
            {"scope": self.scope, "now": now, "reason": reason, "engaged_by": engaged_by},
        )
        self.db.commit()
        self._engaged = True
        logger.warning(
            "kill_switch_engaged", scope=self.scope, reason=reason, engaged_by=engaged_by
        )

    def disengage(self, disengaged_by: str) -> None:
        """Manual re-arm only. No code path in this codebase calls this
        automatically — that guarantee is the entire point of a kill
        switch. `engaged_reason`/`engaged_by` are left as the historical
        record of the last engage event; the disengage itself is always
        logged with who/why via structlog."""
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                UPDATE kill_switch_state
                SET engaged = FALSE, updated_at = :now
                WHERE scope = :scope
                """),
            {"scope": self.scope, "now": now},
        )
        self.db.commit()
        self._engaged = False
        logger.warning("kill_switch_disengaged", scope=self.scope, disengaged_by=disengaged_by)
