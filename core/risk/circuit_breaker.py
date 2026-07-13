"""
CircuitBreaker: in-memory per process (decision #6 — auto-recovering
by nature, unlike KillSwitch, so no DB persistence needed for the
breaker's own state); every trip/clear transition is still logged for
audit via circuit_breaker_event_log.

Deliberately ASYMMETRIC, not a direct reuse of RegimeDetector's
symmetric hysteresis: a circuit breaker's entire purpose is to be a
fail-fast safety gate, so it trips immediately on the first reading at
or above threshold — waiting for confirmation_bars consecutive bad
readings before tripping would defeat the point of a circuit breaker.
Clearing is the risky direction (a market that looks calm for one bar
after a spike may just be a pause), so clearing reuses the exact
confirmation-bar counting mechanism from RegimeDetector's hysteresis:
N consecutive clean readings required before the breaker actually
clears — a single clean reading, or a flapping mix of clean/dirty
readings, never clears it.

Design note (rule 9): the spec's constructor signature is exactly
`(name, threshold, confirmation_bars)` — no db handle. Keeping
CircuitBreaker a pure, dependency-free class (matching that signature
literally, and matching "in-memory per process" in its own docstring)
means it can't write to circuit_breaker_event_log itself. `evaluate()`
instead returns whether THIS call caused a transition
(`CircuitBreakerEvaluation.transitioned`), so a caller with a db
handle — RiskEngine, in step 9 — can persist exactly the transitions
that happened, via `record_circuit_breaker_event()` below, without
CircuitBreaker importing SQLAlchemy or taking a db dependency.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


@dataclass
class CircuitBreakerEvaluation:
    tripped: bool
    transitioned: bool  # True if THIS evaluate() call changed tripped/cleared state
    event_type: str | None  # 'tripped' | 'cleared' | None
    reason: str | None


class CircuitBreaker:
    def __init__(self, name: str, threshold: float, confirmation_bars: int):
        self.name = name
        self.threshold = threshold
        self.confirmation_bars = confirmation_bars
        self.reset()

    def reset(self) -> None:
        self._tripped = False
        self._pending_clear_count = 0

    def evaluate(self, current_value: float) -> bool:
        """Returns True if tripped (after hysteresis-confirmed
        clearing, or immediately on trip). Use evaluate_detailed() if
        the caller needs to know whether THIS call caused a
        trip/clear transition (e.g. to log it)."""
        return self.evaluate_detailed(current_value).tripped

    def evaluate_detailed(self, current_value: float) -> CircuitBreakerEvaluation:
        breached = current_value >= self.threshold

        if breached:
            if not self._tripped:
                self._tripped = True
                self._pending_clear_count = 0
                reason = f"value={current_value} >= threshold={self.threshold}"
                logger.warning("circuit_breaker_tripped", name=self.name, reason=reason)
                return CircuitBreakerEvaluation(
                    tripped=True, transitioned=True, event_type="tripped", reason=reason
                )
            self._pending_clear_count = 0
            return CircuitBreakerEvaluation(
                tripped=True, transitioned=False, event_type=None, reason=None
            )

        if not self._tripped:
            return CircuitBreakerEvaluation(
                tripped=False, transitioned=False, event_type=None, reason=None
            )

        self._pending_clear_count += 1
        if self._pending_clear_count >= self.confirmation_bars:
            self._tripped = False
            self._pending_clear_count = 0
            reason = (
                f"{self.confirmation_bars} consecutive clean readings, last value={current_value}"
            )
            logger.warning("circuit_breaker_cleared", name=self.name, reason=reason)
            return CircuitBreakerEvaluation(
                tripped=False, transitioned=True, event_type="cleared", reason=reason
            )
        return CircuitBreakerEvaluation(
            tripped=True, transitioned=False, event_type=None, reason=None
        )


def record_circuit_breaker_event(
    db: Session, breaker_name: str, event_type: str, reason: str
) -> None:
    """Persist one circuit_breaker_event_log row. A free function, not
    a CircuitBreaker method, so CircuitBreaker itself stays a pure
    in-memory class — see the module docstring's design note."""
    db.execute(
        text("""
            INSERT INTO circuit_breaker_event_log (breaker_name, event_type, reason, occurred_at)
            VALUES (:name, :event_type, :reason, :occurred_at)
            """),
        {
            "name": breaker_name,
            "event_type": event_type,
            "reason": reason,
            "occurred_at": datetime.now(UTC),
        },
    )
    db.commit()
