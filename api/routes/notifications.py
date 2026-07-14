"""
Notifications API (spec section 18/26) — read-only. The in-app feed
reads notification_log, populated by NotificationPersister (a second,
independent EventBus subscriber wired alongside EventGateway in
api/main.py's lifespan — see that module's docstring for why
persistence was added rather than serving history straight off the
WebSocket bridge, which has none). Optional external channels (email/
webhook) are Step 9's notification_preferences settings, not built
here — this endpoint is the in-app feed only.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.notifications import NotificationOut
from core.notifications.notification_log import NotificationLogStore

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    severity: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> list[NotificationOut]:
    store = NotificationLogStore(db)
    records = store.list_recent(limit=limit, offset=offset, severity=severity)
    return [NotificationOut.model_validate(r) for r in records]
