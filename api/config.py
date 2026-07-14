"""
Dashboard API configuration — env-var-driven, matching every other
credential-adjacent module in this project (never a hardcoded secret,
never a plaintext password in a config file).

Single-operator auth (decision #7): exactly one operator identity,
configured via `DASHBOARD_OPERATOR_USERNAME`/`DASHBOARD_OPERATOR_PASSWORD_HASH`
(a bcrypt hash — see `api/auth/session_store.py`'s module docstring for
how to generate one; the plaintext password is never an env var, only
its hash is). `DASHBOARD_ACCOUNT_ID` is the account_id every session
carries, defaulting to "default" — the schema already threads
account_id through, per decision #7, so multi-tenant extension later
is additive.

Deployment target (open decision #3, confirmed with the user): local
dev only for now. `cookie_secure=False` reflects that explicitly —
this must become True the moment this ever runs behind real HTTPS;
flagged here rather than silently left wrong for a future deployment.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardSettings:
    operator_username: str
    operator_password_hash: str
    account_id: str
    session_duration_hours: float
    cookie_secure: bool
    cors_origins: list[str]
    login_rate_limit_attempts: int
    login_rate_limit_window_seconds: float
    enable_event_gateway: bool
    llm_daily_cap_calls: int
    llm_model: str
    llm_api_key_env_var: str


def load_settings() -> DashboardSettings:
    return DashboardSettings(
        operator_username=os.environ.get("DASHBOARD_OPERATOR_USERNAME", "operator"),
        operator_password_hash=os.environ.get("DASHBOARD_OPERATOR_PASSWORD_HASH", ""),
        account_id=os.environ.get("DASHBOARD_ACCOUNT_ID", "default"),
        session_duration_hours=float(os.environ.get("DASHBOARD_SESSION_DURATION_HOURS", "12")),
        # Local dev only (confirmed) — must be True behind real HTTPS.
        cookie_secure=os.environ.get("DASHBOARD_COOKIE_SECURE", "false").lower() == "true",
        cors_origins=os.environ.get("DASHBOARD_CORS_ORIGINS", "http://localhost:5173").split(","),
        login_rate_limit_attempts=int(os.environ.get("DASHBOARD_LOGIN_RATE_LIMIT_ATTEMPTS", "5")),
        login_rate_limit_window_seconds=float(
            os.environ.get("DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
        ),
        # Off by default in the test suite (see tests/test_api/conftest.py)
        # so importing api.main never opens a real Postgres LISTEN/NOTIFY
        # connection just to run an unrelated auth test.
        enable_event_gateway=os.environ.get("DASHBOARD_ENABLE_EVENT_GATEWAY", "true").lower()
        == "true",
        llm_daily_cap_calls=int(os.environ.get("DASHBOARD_LLM_DAILY_CAP_CALLS", "50")),
        llm_model=os.environ.get("DASHBOARD_LLM_MODEL", "claude-sonnet-5"),
        # The env var NAME to read the key from, not the key itself —
        # matches LLMClient's own constructor shape (core/ai_assistant/
        # llm_client.py). No ANTHROPIC_API_KEY is set in this dev
        # environment; LLMClient construction stays safe either way
        # (lazy import/lookup), only a real generate() call fails.
        llm_api_key_env_var=os.environ.get("DASHBOARD_LLM_API_KEY_ENV_VAR", "ANTHROPIC_API_KEY"),
    )
