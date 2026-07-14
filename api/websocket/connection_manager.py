"""
Tracks live, authenticated WebSocket connections keyed by account_id
(spec section 19: "republishing a filtered, account-scoped subset").
Pure bookkeeping — no EventBus/auth knowledge here, so it's trivially
testable without a real WebSocket. `broadcast_to_account()` is
best-effort per connection: one dead/broken socket must never prevent
delivery to the other connections for the same account.
"""

from collections import defaultdict

import structlog
from fastapi import WebSocket

logger = structlog.get_logger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    def connect(self, account_id: str, websocket: WebSocket) -> None:
        self._connections[account_id].add(websocket)

    def disconnect(self, account_id: str, websocket: WebSocket) -> None:
        self._connections[account_id].discard(websocket)
        if not self._connections[account_id]:
            del self._connections[account_id]

    def connection_count(self, account_id: str) -> int:
        return len(self._connections.get(account_id, ()))

    async def broadcast_to_account(self, account_id: str, message: dict) -> None:
        sockets = list(self._connections.get(account_id, ()))
        for socket in sockets:
            try:
                await socket.send_json(message)
            except Exception:
                logger.warning("websocket_send_failed_dropping_connection", account_id=account_id)
                self.disconnect(account_id, socket)
