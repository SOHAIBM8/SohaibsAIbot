"""
Read/write access to notification_log — see schema.sql's comment on
that table for why it exists (LISTEN/NOTIFY has zero persistence of
its own). Written by NotificationPersister (notification_persister.py),
read by the dashboard's Notifications API.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session


@dataclass
class NotificationRecord:
    id: int
    event_type: str
    severity: str
    message: str
    payload: dict
    occurred_at: datetime


class NotificationLogStore:
    def __init__(self, db: Session):
        self.db = db

    def record(
        self, event_type: str, severity: str, message: str, payload: dict, occurred_at: datetime
    ) -> None:
        self.db.execute(
            text("""
                INSERT INTO notification_log
                    (event_type, severity, message, payload, occurred_at)
                VALUES (:event_type, :severity, :message, :payload, :occurred_at)
                """),
            {
                "event_type": event_type,
                "severity": severity,
                "message": message,
                "payload": json.dumps(payload, default=str),
                "occurred_at": occurred_at,
            },
        )
        self.db.commit()

    def list_recent(
        self, limit: int = 50, offset: int = 0, severity: str | None = None
    ) -> list[NotificationRecord]:
        conditions = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if severity is not None:
            conditions.append("severity = :severity")
            params["severity"] = severity
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = (
            self.db.execute(
                text(f"""
                    SELECT id, event_type, severity, message, payload, occurred_at
                    FROM notification_log
                    {where_clause}
                    ORDER BY occurred_at DESC
                    LIMIT :limit OFFSET :offset
                    """),
                params,
            )
            .mappings()
            .all()
        )
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: RowMapping) -> NotificationRecord:
        return NotificationRecord(
            id=row["id"],
            event_type=row["event_type"],
            severity=row["severity"],
            message=row["message"],
            payload=row["payload"],
            occurred_at=row["occurred_at"],
        )
