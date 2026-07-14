"""
Full /api/ws integration test. Uses `with TestClient(app) as client:` —
the only way to trigger FastAPI's lifespan (see conftest.py's plain
`client` fixture, which deliberately does NOT use `with` so unrelated
auth tests never open a real Postgres LISTEN/NOTIFY connection). The
event gateway itself stays disabled here too (DASHBOARD_ENABLE_EVENT_GATEWAY
is "false" by the autouse fixture) — this test proves the route's own
auth + connect/disconnect plumbing against the real ConnectionManager
on app.state. The broadcast-delivery logic itself (resolving an
account, dropping unattributable events, fan-out to multiple sockets)
already has full coverage in test_connection_manager.py and
test_gateway.py against fake sockets — TestClient's WebSocket test
double runs the ASGI app on a separate thread/event loop, which makes
scheduling a real cross-thread broadcast from the test body needlessly
fragile without adding any real assurance beyond what's already proven
there.
"""

import pytest
from starlette.testclient import WebSocketDisconnect

from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME


@pytest.fixture
def ws_client(db):
    from api.auth import router as auth_router_module

    auth_router_module._login_rate_limiter = None
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        yield client


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return response


def test_websocket_rejects_connection_without_a_session(ws_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/api/ws"):
            pass
    assert exc_info.value.code == 4401


def test_websocket_accepts_a_valid_session_and_registers_the_connection(ws_client):
    _login(ws_client)
    manager = ws_client.app.state.connection_manager

    with ws_client.websocket_connect("/api/ws"):
        assert manager.connection_count("test_dashboard_account") == 1

    assert manager.connection_count("test_dashboard_account") == 0


def test_websocket_rejects_an_invalid_session_cookie(ws_client):
    ws_client.cookies.set("dashboard_session", "not-a-real-token")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_client.websocket_connect("/api/ws"):
            pass
    assert exc_info.value.code == 4401
