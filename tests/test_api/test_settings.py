"""
Settings API integration tests against real local Postgres. Credentials
are seeded via the real KeyLifecycleManager.register() write path (a
real LocalDevKMSClient — testnet-only stand-in, never a real cloud
KMS, same as every other credential test in this project).
"""

import pytest
import structlog
from sqlalchemy import text

from core.security.credential_vault import CredentialVault
from core.security.key_lifecycle_manager import KeyLifecycleManager
from core.security.kms_client import LocalDevKMSClient
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

ACCOUNT_ID = "test_dashboard_account"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return response.cookies["dashboard_csrf"]


@pytest.fixture
def seeded_credential(db):
    vault = CredentialVault(LocalDevKMSClient(kek_env_var="TEST_SETTINGS_API_KEK"))
    manager = KeyLifecycleManager(db, vault)
    credential_id = manager.register(
        account_id=ACCOUNT_ID, exchange="binance", api_key="k", api_secret="s", mainnet=False
    )
    yield credential_id
    db.execute(
        text("DELETE FROM encrypted_credentials WHERE credential_id = :c"),
        {"c": credential_id},
    )
    db.commit()


def test_list_credentials_requires_auth(client):
    response = client.get("/api/settings/credentials")
    assert response.status_code == 401


def test_list_credentials_returns_metadata_only(client, seeded_credential):
    _logged_in(client)

    response = client.get("/api/settings/credentials")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["credential_id"] == seeded_credential
    assert entry["exchange"] == "binance"
    assert entry["state"] == "pending_validation"
    # Never leaks ciphertext or key material of any kind.
    assert "encrypted_api_key" not in entry
    assert "encrypted_api_secret" not in entry
    assert "wrapped_dek" not in entry
    assert "api_key" not in entry
    assert "api_secret" not in entry


def test_list_credentials_empty_when_none_registered(client):
    _logged_in(client)
    response = client.get("/api/settings/credentials")
    assert response.status_code == 200
    assert response.json() == []


@pytest.fixture
def cleanup_created_credentials(db):
    yield
    db.execute(text("DELETE FROM encrypted_credentials WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.commit()


def test_create_credential_requires_auth(client):
    response = client.post(
        "/api/settings/credentials",
        json={"exchange": "binance", "api_key": "k", "api_secret": "s"},
    )
    assert response.status_code == 401


def test_create_credential_requires_csrf(client, cleanup_created_credentials):
    _logged_in(client)
    response = client.post(
        "/api/settings/credentials",
        json={"exchange": "binance", "api_key": "k", "api_secret": "s"},
    )
    assert response.status_code == 403


def test_create_credential_rejects_mainnet(client, cleanup_created_credentials):
    csrf_token = _logged_in(client)
    response = client.post(
        "/api/settings/credentials",
        json={"exchange": "binance", "api_key": "k", "api_secret": "s", "mainnet": True},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 400


def test_create_credential_returns_metadata_only(client, cleanup_created_credentials):
    csrf_token = _logged_in(client)

    response = client.post(
        "/api/settings/credentials",
        json={"exchange": "binance", "api_key": "PLAINTEXT-KEY", "api_secret": "PLAINTEXT-SECRET"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["exchange"] == "binance"
    assert body["mainnet"] is False
    assert body["state"] == "pending_validation"
    assert "credential_id" in body
    # Never echoes the plaintext back, ever.
    assert "PLAINTEXT-KEY" not in response.text
    assert "PLAINTEXT-SECRET" not in response.text
    assert "api_key" not in body
    assert "api_secret" not in body


def test_created_credential_then_appears_in_list(client, cleanup_created_credentials):
    csrf_token = _logged_in(client)
    create_response = client.post(
        "/api/settings/credentials",
        json={"exchange": "kraken", "api_key": "k", "api_secret": "s"},
        headers={"X-CSRF-Token": csrf_token},
    )
    credential_id = create_response.json()["credential_id"]

    list_response = client.get("/api/settings/credentials")

    ids = [c["credential_id"] for c in list_response.json()]
    assert credential_id in ids


def test_create_credential_never_logs_plaintext(client, cleanup_created_credentials):
    """Decision #8's discipline (Stage 3), applied identically at this
    new entry point — structlog.testing.capture_logs() catches every
    log call regardless of level, not fooled by an INFO filter hiding
    a plaintext value at DEBUG."""
    csrf_token = _logged_in(client)
    sentinel_key = "PLAINTEXT-SENTINEL-DASHBOARD-API-KEY-7f3e"
    sentinel_secret = "PLAINTEXT-SENTINEL-DASHBOARD-API-SECRET-2c91"

    with structlog.testing.capture_logs() as captured:
        response = client.post(
            "/api/settings/credentials",
            json={"exchange": "binance", "api_key": sentinel_key, "api_secret": sentinel_secret},
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 201
    log_text = str(captured)
    assert sentinel_key not in log_text
    assert sentinel_secret not in log_text


@pytest.fixture
def cleanup_notification_prefs(db):
    yield
    db.execute(
        text("DELETE FROM notification_preferences WHERE account_id = :a"), {"a": ACCOUNT_ID}
    )
    db.commit()


def test_get_notification_preferences_requires_auth(client):
    response = client.get("/api/settings/notifications")
    assert response.status_code == 401


def test_get_notification_preferences_returns_defaults(client, cleanup_notification_prefs):
    _logged_in(client)
    response = client.get("/api/settings/notifications")
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == ACCOUNT_ID
    assert body["email_enabled"] is False
    assert body["notify_on_kill_switch"] is True


def test_update_notification_preferences_requires_csrf(client, cleanup_notification_prefs):
    _logged_in(client)
    response = client.put("/api/settings/notifications", json={"email_enabled": True})
    assert response.status_code == 403


def test_update_notification_preferences_persists(client, cleanup_notification_prefs):
    csrf_token = _logged_in(client)

    response = client.put(
        "/api/settings/notifications",
        json={
            "email_enabled": True,
            "email_address": "ops@example.com",
            "notify_on_drawdown_breach": False,
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email_enabled"] is True
    assert body["email_address"] == "ops@example.com"
    assert body["notify_on_drawdown_breach"] is False

    second = client.get("/api/settings/notifications")
    assert second.json()["email_enabled"] is True
