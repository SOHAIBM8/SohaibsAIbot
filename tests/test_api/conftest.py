"""
Shared fixtures for API tests. Runs against real local Postgres (spec
section 25: "backend API integration tests against a real test
database" — this project's standing no-mocks-for-DB practice applies
here too), using FastAPI's TestClient (a real HTTP client dispatched
in-process, not a mock of the app).
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.db import SessionLocal

TEST_OPERATOR_USERNAME = "test_operator"
TEST_OPERATOR_PASSWORD = "test-password-12345"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _dashboard_env(monkeypatch):
    password_hash = bcrypt.hashpw(TEST_OPERATOR_PASSWORD.encode(), bcrypt.gensalt()).decode()
    monkeypatch.setenv("DASHBOARD_OPERATOR_USERNAME", TEST_OPERATOR_USERNAME)
    monkeypatch.setenv("DASHBOARD_OPERATOR_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("DASHBOARD_ACCOUNT_ID", "test_dashboard_account")
    monkeypatch.setenv("DASHBOARD_SESSION_DURATION_HOURS", "12")
    monkeypatch.setenv("DASHBOARD_LOGIN_RATE_LIMIT_ATTEMPTS", "5")
    monkeypatch.setenv("DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
    # Off by default — most API tests have nothing to do with the
    # WebSocket gateway and shouldn't pay for (or risk interference
    # from) a real Postgres LISTEN/NOTIFY thread. Gateway-specific
    # tests override this explicitly.
    monkeypatch.setenv("DASHBOARD_ENABLE_EVENT_GATEWAY", "false")


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM dashboard_sessions WHERE account_id = 'test_dashboard_account'")
        )
        session.commit()
        session.close()


@pytest.fixture
def client(db):
    # Import after env vars are set (autouse _dashboard_env runs first)
    # so api.main's module-level load_settings() sees the test config.
    from api.auth import router as auth_router_module

    auth_router_module._login_rate_limiter = None  # fresh rate-limit window per test
    from api.main import app

    return TestClient(app)
