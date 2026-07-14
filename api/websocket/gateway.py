"""
Bridges the internal EventBus to WebSocket connections (spec section
2/19). The EventBus's handler callback fires SYNCHRONOUSLY on its own
background thread (`PostgresEventBus`'s LISTEN/NOTIFY listener thread)
— broadcasting to WebSocket connections is async and must run on the
FastAPI/uvicorn event loop, so this class captures that loop at
start() time and schedules each broadcast onto it via
`asyncio.run_coroutine_threadsafe()`. Same cross-thread-to-event-loop
bridging shape already used elsewhere in this project (background
thread with a synchronous callback, work scheduled onto an owning event
loop) — not a new pattern.
"""

import asyncio

import structlog

from api.websocket.account_resolver import OrderAccountResolver
from api.websocket.connection_manager import ConnectionManager
from core.ingestion.event_bus import EventBus

logger = structlog.get_logger(__name__)

# The execution/risk event types the dashboard cares about (spec
# section 19: "orders, fills, risk decisions, regime changes,
# notifications"). Not exhaustive of every event type ever defined in
# this project — scoped to what a live dashboard actually displays.
DEFAULT_EVENT_TYPES = [
    "OrderSubmitted",
    "OrderFilled",
    "OrderRejected",
    "OrderCancelled",
    "PaperFillSimulated",
    "RiskDecisionMade",
    "KillSwitchEngaged",
    "KillSwitchDisengaged",
    "DrawdownTierChanged",
    "CredentialValidationFailed",
    "ArmingStateChanged",
    "ArmingExpired",
]


class EventGateway:
    def __init__(
        self,
        event_bus: EventBus,
        connection_manager: ConnectionManager,
        account_resolver: OrderAccountResolver,
        event_types: list[str] | None = None,
    ):
        self.event_bus = event_bus
        self.connection_manager = connection_manager
        self.account_resolver = account_resolver
        self.event_types = event_types or DEFAULT_EVENT_TYPES
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Must be called from within a running asyncio event loop
        (e.g. FastAPI's lifespan startup) — captures that loop so the
        EventBus's background-thread callback can schedule broadcasts
        onto it."""
        self._loop = asyncio.get_running_loop()
        for event_type in self.event_types:
            self.event_bus.subscribe(event_type, self._on_event)

    def _on_event(self, payload: dict) -> None:
        # Runs on the EventBus's own thread — never touches WebSocket
        # connections directly, only schedules the async broadcast.
        if self._loop is None:
            logger.warning("event_gateway_event_before_start", payload=payload)
            return
        account_id = self.account_resolver.resolve(payload)
        if account_id is None:
            return  # unattributable event — never broadcast to everyone
        asyncio.run_coroutine_threadsafe(
            self.connection_manager.broadcast_to_account(account_id, payload), self._loop
        )
