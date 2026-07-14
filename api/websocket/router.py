"""
GET /api/ws — the one WebSocket endpoint (spec section 4/19). Auth via
the SAME session cookie used for REST endpoints — the browser sends
cookies automatically on the WS handshake's HTTP upgrade request, so
there is no separate WebSocket-specific auth scheme to build or keep
in sync with the REST one.

The shared `ConnectionManager` lives on `app.state` (set once at
startup in `api/main.py`) rather than as a route closure — the
idiomatic FastAPI way to share one instance between this router and
the `EventGateway`.
"""

from datetime import timedelta

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth.dependencies import SESSION_COOKIE_NAME
from api.auth.session_store import SessionStore
from api.config import load_settings
from core.db import SessionLocal

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    settings = load_settings()
    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        await websocket.close(code=4401)
        return

    db = SessionLocal()
    try:
        store = SessionStore(db, timedelta(hours=settings.session_duration_hours))
        session = store.validate(raw_token)
    finally:
        db.close()

    if session is None:
        await websocket.close(code=4401)
        return

    connection_manager = websocket.app.state.connection_manager
    await websocket.accept()
    connection_manager.connect(session.account_id, websocket)
    logger.info("websocket_connected", account_id=session.account_id)
    try:
        while True:
            # The client never needs to send anything meaningful here —
            # this loop exists purely to detect disconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(session.account_id, websocket)
        logger.info("websocket_disconnected", account_id=session.account_id)
