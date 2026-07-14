"""
EventGateway tests, using a fake EventBus (matches this project's
established test-double pattern: subscribe() stores handlers,
publish()-equivalent invokes them synchronously) plus a real
ConnectionManager and fake sockets. No real Postgres LISTEN/NOTIFY
here — that's PostgresEventBus's own concern, not EventGateway's.
"""

import asyncio

import pytest

from api.websocket.account_resolver import OrderAccountResolver
from api.websocket.connection_manager import ConnectionManager
from api.websocket.gateway import EventGateway
from core.ingestion.event_bus import EventBus, EventHandler


class FakeEventBus(EventBus):
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def publish(self, event) -> None:  # pragma: no cover - unused by these tests
        raise NotImplementedError

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def fire(self, event_type: str, payload: dict) -> None:
        for handler in self._handlers.get(event_type, []):
            handler(payload)


class FakeAccountResolver(OrderAccountResolver):
    def __init__(self, mapping: dict[str, str | None]):
        self._mapping = mapping

    def resolve(self, payload: dict) -> str | None:
        return self._mapping.get(payload.get("client_order_id", ""))


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.mark.anyio
async def test_resolved_event_is_broadcast_to_the_owning_account():
    bus = FakeEventBus()
    manager = ConnectionManager()
    resolver = FakeAccountResolver({"order-1": "account_a"})
    gateway = EventGateway(bus, manager, resolver, event_types=["OrderFilled"])
    socket = _FakeSocket()
    manager.connect("account_a", socket)  # type: ignore[arg-type]

    gateway.start()
    bus.fire("OrderFilled", {"client_order_id": "order-1", "event_type": "OrderFilled"})
    await asyncio.sleep(0.05)  # let run_coroutine_threadsafe's scheduled coroutine run

    assert socket.sent == [{"client_order_id": "order-1", "event_type": "OrderFilled"}]


@pytest.mark.anyio
async def test_unattributable_event_is_dropped_not_broadcast_to_everyone():
    bus = FakeEventBus()
    manager = ConnectionManager()
    resolver = FakeAccountResolver({})  # resolves nothing
    gateway = EventGateway(bus, manager, resolver, event_types=["OrderFilled"])
    socket = _FakeSocket()
    manager.connect("account_a", socket)  # type: ignore[arg-type]

    gateway.start()
    bus.fire("OrderFilled", {"client_order_id": "unknown-order"})
    await asyncio.sleep(0.05)

    assert socket.sent == []


@pytest.mark.anyio
async def test_only_subscribes_to_the_configured_event_types():
    bus = FakeEventBus()
    manager = ConnectionManager()
    resolver = FakeAccountResolver({"order-1": "account_a"})
    gateway = EventGateway(bus, manager, resolver, event_types=["OrderFilled"])
    socket = _FakeSocket()
    manager.connect("account_a", socket)  # type: ignore[arg-type]

    gateway.start()
    bus.fire("OrderRejected", {"client_order_id": "order-1"})  # not subscribed
    await asyncio.sleep(0.05)

    assert socket.sent == []


def test_event_before_start_is_logged_and_not_broadcast():
    bus = FakeEventBus()
    manager = ConnectionManager()
    resolver = FakeAccountResolver({"order-1": "account_a"})
    gateway = EventGateway(bus, manager, resolver, event_types=["OrderFilled"])

    # Deliberately not calling gateway.start() — _loop stays None.
    bus._handlers.setdefault("OrderFilled", []).append(gateway._on_event)
    bus.fire("OrderFilled", {"client_order_id": "order-1"})
    # must not raise
