"""
Pydantic schemas mirroring core.notifications.notification_log's
NotificationRecord field-for-field (spec section 4).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    severity: str
    message: str
    payload: dict
    occurred_at: datetime
