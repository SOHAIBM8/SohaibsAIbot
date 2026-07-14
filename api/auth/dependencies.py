"""
FastAPI dependencies every protected route uses: `get_current_session`
(401 if not authenticated) and `require_csrf` (403 on any mutating
request missing/mismatching the CSRF double-submit token). Every
route module built in later steps depends on `get_current_session` —
this is the one place "is this request authenticated" is decided.
"""

from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from api.auth.csrf import csrf_check_required, validate_csrf
from api.auth.session_store import DashboardSession, SessionStore
from api.config import DashboardSettings, load_settings
from api.db import get_db

SESSION_COOKIE_NAME = "dashboard_session"


def get_settings() -> DashboardSettings:
    return load_settings()


def get_current_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
) -> DashboardSession:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    store = SessionStore(db, timedelta(hours=settings.session_duration_hours))
    session = store.validate(raw_token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired or invalid"
        )

    if csrf_check_required(request) and not validate_csrf(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")

    return session
