"""
Subscribes to the same internal EventBus api/websocket/gateway.py's
EventGateway does, but persists instead of (or alongside) broadcasting
— a second, independent subscriber on the same bus, not a change to
EventGateway's own responsibility. Mirrors EventGateway's shape
deliberately (fixed event-type list, a callback that runs on the
EventBus's own thread) since it's solving the same
"observe published events" problem, just for a different purpose.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from core.ingestion.event_bus import EventBus
from core.notifications.notification_log import NotificationLogStore
from core.notifications.severity import NOTIFICATION_EVENT_TYPES, SEVERITY_BY_EVENT_TYPE

logger = structlog.get_logger(__name__)

# One short, human-readable line per event type, built only from
# fields real event dataclasses actually carry (core/risk/events.py,
# core/security/events.py) — never a fabricated fact.
_MESSAGE_BUILDERS: dict[str, Callable[[dict], str]] = {
    "KillSwitchEngaged": lambda p: f"Kill switch engaged by {p.get('engaged_by', '?')}: "
    f"{p.get('reason', '')}",
    "KillSwitchDisengaged": lambda p: f"Kill switch disengaged by {p.get('disengaged_by', '?')}",
    "CredentialValidationFailed": lambda p: (
        f"Credential {p.get('credential_id', '?')} failed validation: {p.get('reason', '')}"
    ),
    "EmergencyRevocationTriggered": lambda p: (
        f"Credential {p.get('credential_id', '?')} emergency-revoked by "
        f"{p.get('triggered_by', '?')}: {p.get('reason', '')}"
    ),
    "DrawdownTierChanged": lambda p: (
        f"Drawdown tier changed {p.get('previous_tier', '?')} -> {p.get('new_tier', '?')} "
        f"({p.get('current_drawdown_pct', 0.0):.1%} drawdown)"
    ),
    "DailyLossLimitBreached": lambda p: (
        f"Daily loss limit breached on {p.get('date', '?')}: "
        f"{p.get('realized_pnl_pct', 0.0):.1%} realized PnL"
    ),
    "CircuitBreakerTripped": lambda p: (
        f"Circuit breaker '{p.get('breaker_name', '?')}' tripped: {p.get('reason', '')}"
    ),
    "CircuitBreakerCleared": lambda p: f"Circuit breaker '{p.get('breaker_name', '?')}' cleared",
    "ArmingExpired": lambda p: (
        f"Arming expired for strategy {p.get('strategy_id', '?')} on {p.get('exchange', '?')}"
    ),
}


class NotificationPersister:
    def __init__(self, event_bus: EventBus, store_factory: Callable[[], NotificationLogStore]):
        # store_factory (not a bound NotificationLogStore instance): the
        # EventBus callback fires on its own background thread, same
        # cross-thread constraint api/websocket/account_resolver.py's
        # OrderAccountResolver was built around — a store needs a fresh,
        # short-lived db session per event, not one session shared
        # across the whole process lifetime.
        self.event_bus = event_bus
        self.store_factory = store_factory

    def start(self) -> None:
        for event_type in NOTIFICATION_EVENT_TYPES:
            self.event_bus.subscribe(event_type, self._on_event)

    def _on_event(self, payload: dict) -> None:
        event_type = payload.get("event_type")
        if event_type not in SEVERITY_BY_EVENT_TYPE:
            return
        severity = SEVERITY_BY_EVENT_TYPE[event_type]
        message_builder = _MESSAGE_BUILDERS.get(event_type, lambda p: event_type)
        message = message_builder(payload)
        occurred_at = payload.get("occurred_at")

        store = self.store_factory()
        try:
            occurred_at_parsed = (
                datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(UTC)
            )
            store.record(
                event_type=event_type,
                severity=severity,
                message=message,
                payload=payload,
                occurred_at=occurred_at_parsed,
            )
        except Exception:
            logger.exception("notification_persist_failed", event_type=event_type)
        finally:
            store.db.close()
