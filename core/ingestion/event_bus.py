"""
EventBus interface + a Postgres LISTEN/NOTIFY implementation (spec
4.9). The interface exists so Kafka/Redis Streams/NATS can replace the
transport later without any publisher or subscriber code changing —
that's the entire point of depending on EventBus, not PostgresEventBus,
everywhere else in the ingestion component.

Design note (rule 9, generalized while wiring up docs/risk_engine_spec.md):
`publish()` originally accepted only `IngestionEvent`. The Risk Engine
(core/risk/risk_engine.py) needs the SAME bus/transport for its own
events (RiskDecisionMade, KillSwitchEngaged, ...), which have nothing
to do with ingestion. `PostgresEventBus.publish()` only ever touches
`event.event_type` and `event.to_dict()` — it never actually depended
on IngestionEvent specifically, just its shape. Loosening the type hint
to the structural `EventLike` Protocol below makes that already-true
fact explicit, with zero behavior change and no change needed to any
existing IngestionEvent subclass (dataclasses satisfy a Protocol
structurally, not by inheritance). This is exactly the kind of reuse
the interface's own docstring says it exists for.

Design note: core/db.py's docstring says "no other module should
construct a connection string or import psycopg2 directly" — LISTEN/
NOTIFY is the one exception that needs a raw, autocommit DBAPI
connection with select()-based polling, which SQLAlchemy's Core/ORM
layer doesn't expose. PostgresEventBus gets that raw connection via
`core.db.engine.raw_connection()` rather than opening a second
connection path — it's still the same engine, same DATABASE_URL, same
pool, just unwrapped for the one API (LISTEN) that requires it.
"""

import json
import select
import threading
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from core.db import engine

logger = structlog.get_logger(__name__)

EventHandler = Callable[[dict], None]


class EventLike(Protocol):
    @property
    def event_type(self) -> str: ...

    def to_dict(self) -> dict: ...


class EventBus(ABC):
    @abstractmethod
    def publish(self, event: EventLike) -> None: ...

    @abstractmethod
    def subscribe(self, event_type: str, handler: EventHandler) -> None: ...


class PostgresEventBus(EventBus):
    """LISTEN/NOTIFY implementation. `start()` must be called before
    subscribers will receive anything; `close()` stops the listener
    thread. Safe to publish without ever calling start() — publish
    doesn't require a listener."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._conn: Any = None
        self._listener_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False

    def publish(self, event: EventLike) -> None:
        raw = engine.raw_connection()
        try:
            dbapi_conn: Any = raw.driver_connection
            dbapi_conn.autocommit = True
            cursor = dbapi_conn.cursor()
            cursor.execute(
                "SELECT pg_notify(%s, %s)",
                (event.event_type, json.dumps(event.to_dict(), default=str)),
            )
            cursor.close()
        finally:
            raw.close()

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)
        if self._started:
            self._listen(event_type)

    def start(self) -> None:
        if self._started:
            return
        raw = engine.raw_connection()
        self._conn = raw.driver_connection
        # detach(): the listener holds this connection open indefinitely
        # (it's the socket the background thread selects/polls on).
        # Without detaching, the ConnectionFairy going out of scope here
        # would get garbage-collected and silently check the connection
        # back into the pool — where a later engine.raw_connection() call
        # (e.g. from publish(), or any unrelated session) could be handed
        # the same physical connection while the listener thread is still
        # using it, corrupting both callers. Must grab driver_connection
        # before detaching — detach() clears the fairy's reference to it.
        raw.detach()
        self._conn.autocommit = True
        for event_type in self._handlers:
            self._listen(event_type)
        self._started = True
        self._stop.clear()
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=2)
        if self._conn is not None:
            self._conn.close()
        self._started = False

    def _listen(self, event_type: str) -> None:
        if self._conn is None:
            return
        cursor = self._conn.cursor()
        # Quoted identifier: LISTEN folds an unquoted channel name to
        # lowercase, but pg_notify()'s channel argument is a plain
        # string and stays case-sensitive — without quoting here,
        # "CandlesIngested" would LISTEN on "candlesingested" and never
        # match a NOTIFY sent on the exact-case channel.
        cursor.execute(f'LISTEN "{event_type}";')
        cursor.close()

    def _listen_loop(self) -> None:
        assert self._conn is not None
        while not self._stop.is_set():
            if not select.select([self._conn], [], [], 1)[0]:
                continue
            self._conn.poll()
            while self._conn.notifies:
                notify = self._conn.notifies.pop(0)
                try:
                    payload = json.loads(notify.payload)
                except ValueError:
                    logger.warning("event_bus_bad_payload", channel=notify.channel)
                    continue
                for handler in self._handlers.get(notify.channel, []):
                    try:
                        handler(payload)
                    except Exception:
                        logger.exception("event_bus_handler_failed", channel=notify.channel)
