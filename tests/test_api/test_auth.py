"""
Tests run against real local Postgres via FastAPI's TestClient (a real
in-process HTTP client, not a mock of the app) — spec section 25.
"""

from sqlalchemy import text

from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME


def test_login_with_correct_credentials_succeeds_and_sets_cookies(client, db):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "test_dashboard_account"
    assert "dashboard_session" in response.cookies
    assert "dashboard_csrf" in response.cookies

    row = (
        db.execute(
            text("SELECT account_id FROM dashboard_sessions WHERE account_id = :a"),
            {"a": "test_dashboard_account"},
        )
        .mappings()
        .first()
    )
    assert row is not None


def test_login_with_wrong_password_is_rejected(client, db):
    response = client.post(
        "/api/auth/login", json={"username": TEST_OPERATOR_USERNAME, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert "dashboard_session" not in response.cookies


def test_login_with_wrong_username_is_rejected(client, db):
    response = client.post(
        "/api/auth/login", json={"username": "someone_else", "password": TEST_OPERATOR_PASSWORD}
    )
    assert response.status_code == 401


def test_me_without_a_session_is_401(client, db):
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_me_with_a_valid_session_returns_account_id(client, db):
    client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["account_id"] == "test_dashboard_account"


def test_logout_revokes_the_session(client, db):
    login_response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    csrf_token = login_response.cookies["dashboard_csrf"]

    logout_response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert logout_response.status_code == 200

    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 401


def test_logout_without_csrf_header_is_rejected(client, db):
    client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )

    response = client.post("/api/auth/logout")  # no X-CSRF-Token header

    assert response.status_code == 403
    # the session must still be valid — logout was refused, not silently applied
    assert client.get("/api/auth/me").status_code == 200


def test_logout_with_wrong_csrf_token_is_rejected(client, db):
    client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )

    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": "not-the-real-token"})

    assert response.status_code == 403


def test_login_rate_limit_blocks_after_too_many_attempts(client, db):
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={"username": TEST_OPERATOR_USERNAME, "password": "wrong"}
        )
        assert response.status_code == 401

    blocked_response = client.post(
        "/api/auth/login", json={"username": TEST_OPERATOR_USERNAME, "password": "wrong"}
    )
    assert blocked_response.status_code == 429


def test_login_rate_limit_does_not_block_a_correct_login_after_failures_within_budget(client, db):
    for _ in range(3):
        client.post(
            "/api/auth/login", json={"username": TEST_OPERATOR_USERNAME, "password": "wrong"}
        )

    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200


def test_session_token_stored_in_db_is_hashed_not_the_raw_cookie_value(client, db):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    raw_token = response.cookies["dashboard_session"]

    row = (
        db.execute(
            text("SELECT session_id FROM dashboard_sessions WHERE account_id = :a"),
            {"a": "test_dashboard_account"},
        )
        .mappings()
        .first()
    )

    assert row["session_id"] != raw_token  # never the raw value
    assert len(row["session_id"]) == 64  # sha256 hex digest length


def test_security_headers_are_present_on_every_response(client, db):
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_health_endpoint_requires_no_auth(client, db):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
