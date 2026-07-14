"""
Per-account notification channel preferences (docs/dashboard_ui_spec.md
section 18/27). Unlike account_snapshots/positions, "no row exists
yet" is a normal, expected state here (every account starts with no
preferences configured) — get() returns sensible defaults rather than
raising or surfacing an "unavailable" flag, since there's no real gap
to flag: an account that never configured notifications simply has
notifications off, which is exactly what the defaults represent.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class NotificationPreferences:
    account_id: str
    email_enabled: bool = False
    email_address: str | None = None
    webhook_enabled: bool = False
    webhook_url: str | None = None
    notify_on_kill_switch: bool = True
    notify_on_credential_validation_failed: bool = True
    notify_on_drawdown_breach: bool = True
    updated_at: datetime | None = None


class NotificationPreferencesStore:
    def __init__(self, db: Session):
        self.db = db

    def get(self, account_id: str) -> NotificationPreferences:
        row = (
            self.db.execute(
                text("""
                    SELECT account_id, email_enabled, email_address, webhook_enabled,
                           webhook_url, notify_on_kill_switch,
                           notify_on_credential_validation_failed, notify_on_drawdown_breach,
                           updated_at
                    FROM notification_preferences WHERE account_id = :account_id
                    """),
                {"account_id": account_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return NotificationPreferences(account_id=account_id)
        return NotificationPreferences(**row)

    def upsert(self, prefs: NotificationPreferences) -> NotificationPreferences:
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                INSERT INTO notification_preferences
                    (account_id, email_enabled, email_address, webhook_enabled, webhook_url,
                     notify_on_kill_switch, notify_on_credential_validation_failed,
                     notify_on_drawdown_breach, updated_at)
                VALUES
                    (:account_id, :email_enabled, :email_address, :webhook_enabled, :webhook_url,
                     :notify_on_kill_switch, :notify_on_credential_validation_failed,
                     :notify_on_drawdown_breach, :updated_at)
                ON CONFLICT (account_id) DO UPDATE SET
                    email_enabled = EXCLUDED.email_enabled,
                    email_address = EXCLUDED.email_address,
                    webhook_enabled = EXCLUDED.webhook_enabled,
                    webhook_url = EXCLUDED.webhook_url,
                    notify_on_kill_switch = EXCLUDED.notify_on_kill_switch,
                    notify_on_credential_validation_failed =
                        EXCLUDED.notify_on_credential_validation_failed,
                    notify_on_drawdown_breach = EXCLUDED.notify_on_drawdown_breach,
                    updated_at = EXCLUDED.updated_at
                """),
            {
                "account_id": prefs.account_id,
                "email_enabled": prefs.email_enabled,
                "email_address": prefs.email_address,
                "webhook_enabled": prefs.webhook_enabled,
                "webhook_url": prefs.webhook_url,
                "notify_on_kill_switch": prefs.notify_on_kill_switch,
                "notify_on_credential_validation_failed": (
                    prefs.notify_on_credential_validation_failed
                ),
                "notify_on_drawdown_breach": prefs.notify_on_drawdown_breach,
                "updated_at": now,
            },
        )
        self.db.commit()
        return self.get(prefs.account_id)
