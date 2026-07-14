"""
POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me.
Single-operator auth (decision #7): credentials are checked against
`DashboardSettings.operator_username`/`operator_password_hash`
(env-var-configured), never a users table — there is exactly one
operator identity in V1.
"""

import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth.csrf import CSRF_COOKIE_NAME, generate_csrf_token, validate_csrf
from api.auth.dependencies import SESSION_COOKIE_NAME, get_current_session, get_settings
from api.auth.rate_limiter import LoginRateLimiter
from api.auth.session_store import DashboardSession, SessionStore, verify_operator_password
from api.config import DashboardSettings
from api.db import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# One limiter instance shared across requests to this process — a
# fresh instance per request would reset the window on every call,
# defeating the whole point.
_login_rate_limiter: LoginRateLimiter | None = None


def _get_rate_limiter(settings: DashboardSettings) -> LoginRateLimiter:
    global _login_rate_limiter
    if _login_rate_limiter is None:
        _login_rate_limiter = LoginRateLimiter(
            max_attempts=settings.login_rate_limit_attempts,
            window_seconds=settings.login_rate_limit_window_seconds,
        )
    return _login_rate_limiter


class LoginRequest(BaseModel):
    username: str
    password: str


class SessionInfo(BaseModel):
    account_id: str
    expires_at: str


@router.post("/login", response_model=SessionInfo)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
) -> SessionInfo:
    limiter = _get_rate_limiter(settings)
    client_key = request.client.host if request.client else "unknown"
    if not limiter.check(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too many login attempts"
        )

    valid_username = hmac.compare_digest(body.username, settings.operator_username)
    valid_password = verify_operator_password(body.password, settings.operator_password_hash)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    limiter.reset(client_key)

    store = SessionStore(db, timedelta(hours=settings.session_duration_hours))
    raw_token = store.create(settings.account_id)
    csrf_token = generate_csrf_token()

    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int(settings.session_duration_hours * 3600),
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,  # must be JS-readable — that's the whole point of double-submit
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int(settings.session_duration_hours * 3600),
    )

    session = store.validate(raw_token)
    assert session is not None  # just created it
    return SessionInfo(account_id=session.account_id, expires_at=session.expires_at.isoformat())


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
) -> dict:
    # Logout is mutating (decision #24: CSRF on ALL mutating endpoints,
    # no carve-outs) — but only enforced when a session cookie is
    # actually present; a request with no session to revoke has
    # nothing for CSRF to protect.
    if request.cookies.get(SESSION_COOKIE_NAME) is not None and not validate_csrf(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")

    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is not None:
        store = SessionStore(db, timedelta(hours=settings.session_duration_hours))
        store.revoke(raw_token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me", response_model=SessionInfo)
def me(session: DashboardSession = Depends(get_current_session)) -> SessionInfo:
    return SessionInfo(account_id=session.account_id, expires_at=session.expires_at.isoformat())
