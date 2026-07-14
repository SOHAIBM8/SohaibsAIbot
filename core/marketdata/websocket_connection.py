"""
One connection per exchange. Stage 1: a fake/simulated feed for paper
trading's market data needs; real exchange websockets are Stage 2.

Exponential backoff reconnect, same taxonomy/shape as ingestion's
RetryPolicy (core/ingestion/retry_policy.py) — base delay doubling per
attempt, capped, with jitter so many reconnecting connections don't
all retry in lockstep. Heartbeat: if no message arrives within
heartbeat_timeout_s, the connection is treated as stale and force-
reconnected rather than left silently hanging — a WebSocket that LOOKS
open but has stopped delivering data is worse than one that visibly
drops, because nothing else would know to route around it.

Runs the actual (asyncio-based) `websockets` client on a dedicated
background thread with its own event loop, exposing a synchronous
public API (connect/close/is_alive) — the same "background thread,
synchronous public interface" shape already established by
PostgresEventBus (core/ingestion/event_bus.py) for LISTEN/NOTIFY,
rather than introducing asyncio as a calling convention anywhere else
in an otherwise fully synchronous codebase.

Design note (rule 9, added for docs/execution_engine_stage2_spec.md
step 4): `on_open` is an optional callback invoked once per successful
connection (including every reconnect), returning a message to send
immediately after connecting. Added because Binance deprecated its
listenKey-based user data stream (REST `POST /api/v3/userDataStream`,
confirmed returning 410 Gone against real testnet during this build)
in favor of a signed subscription sent as the FIRST message over the
WebSocket API connection itself — a plain "connect to a
pre-authorized URL" model no longer covers this. Kept generic (any
caller-supplied message, not Binance-specific) since this class has no
business knowing about Binance's request shapes.
"""

import asyncio
import random
import threading
import time
from collections.abc import Callable

import structlog
import websockets

logger = structlog.get_logger(__name__)


class WebSocketConnection:
    def __init__(
        self,
        url: str,
        on_message: Callable[[str], None],
        heartbeat_timeout_s: float,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        connect_timeout_s: float = 10.0,
        rand: Callable[[], float] | None = None,
        on_open: Callable[[], str] | None = None,
    ):
        self.url = url
        self.on_message = on_message
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.on_open = on_open
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        # Deliberately separate from heartbeat_timeout_s: connecting and
        # staying-alive-once-connected are different concerns. Reusing
        # heartbeat_timeout_s here meant a small heartbeat window (fine
        # for detecting staleness) also made every failed CONNECTION
        # attempt take that long to time out — on Windows in particular,
        # a refused/unbound port doesn't fail fast, so this silently
        # made backoff look broken (attempts were still in flight, not
        # actually tight-looping).
        self.connect_timeout_s = connect_timeout_s
        self._rand = rand or random.random

        self._alive = False
        self._last_message_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempt = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_ws: websockets.ClientConnection | None = None

        # Test/observability hooks — not behavioral, just visibility
        # into reconnect activity (used to assert backoff is working).
        self.reconnect_count = 0
        self.backoff_delays: list[float] = []

    def connect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        # An in-flight ws.recv() only notices _stop on its NEXT loop
        # iteration, which could be up to heartbeat_timeout_s away —
        # actively close the live socket from its own event loop
        # thread so a blocked recv() unblocks immediately instead of
        # potentially outliving this method's own join timeout.
        if self._loop is not None and self._current_ws is not None:
            ws = self._current_ws
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(ws.close(), loop=self._loop)
            )
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._alive = False

    def is_alive(self) -> bool:
        if not self._alive:
            return False
        if self._last_message_at is None:
            return False
        return (time.monotonic() - self._last_message_at) < self.heartbeat_timeout_s

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_async())
        finally:
            self._loop.close()
            self._loop = None

    async def _run_async(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
            except Exception as exc:
                logger.warning("websocket_connection_dropped", url=self.url, error=str(exc))
            self._alive = False
            if self._stop.is_set():
                break
            self.reconnect_count += 1
            backoff = min(self.base_backoff_s * (2**self._attempt), self.max_backoff_s)
            backoff *= 0.5 + self._rand()  # jitter: 0.5x-1.5x
            self._attempt += 1
            self.backoff_delays.append(backoff)
            await asyncio.sleep(backoff)

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(self.url, open_timeout=self.connect_timeout_s) as ws:
            self._current_ws = ws
            self._alive = True
            self._last_message_at = time.monotonic()
            self._attempt = 0  # a successful connection resets backoff
            if self.on_open is not None:
                # Fires on every successful connection, including
                # reconnects — a fresh connection needs a fresh
                # subscribe/auth message just as much as the first one.
                await ws.send(self.on_open())
            try:
                while not self._stop.is_set():
                    try:
                        message = await asyncio.wait_for(
                            ws.recv(), timeout=self.heartbeat_timeout_s
                        )
                    except TimeoutError as exc:
                        raise ConnectionError(
                            f"no message within heartbeat_timeout_s={self.heartbeat_timeout_s}"
                        ) from exc
                    self._last_message_at = time.monotonic()
                    self.on_message(message if isinstance(message, str) else message.decode())
            finally:
                self._current_ws = None
