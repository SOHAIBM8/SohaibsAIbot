"""
Postgres-backed session store (open decision #1, confirmed: server-
side session table, not stateless JWT). Only a SHA-256 hash of the raw
session token is ever persisted — the raw token exists only in the
httpOnly cookie on the client and in memory for the single request
that issues it. Mirrors `credential_audit_log`'s "the sensitive value
itself is never the thing stored" discipline from Stage 3.

To generate an operator password hash for `DASHBOARD_OPERATOR_PASSWORD_HASH`:
    python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import bcrypt
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

_TOKEN_BYTES = 32


@dataclass
class DashboardSession:
    session_id: str  # hashed — never the raw token
    account_id: str
    created_at: datetime
    last_active_at: datetime
    expires_at: datetime


def verify_operator_password(plaintext_password: str, password_hash: str) -> bool:
    if not password_hash:
        return False  # no hash configured — refuse, never fall back to "any password works"
    return bcrypt.checkpw(plaintext_password.encode(), password_hash.encode())


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class SessionStore:
    def __init__(self, db: Session, session_duration: timedelta):
        self.db = db
        self.session_duration = session_duration

    def create(self, account_id: str) -> str:
        """Returns the RAW token — the only time it ever exists outside
        the cookie it's about to be set into. Only its hash is stored."""
        raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = datetime.now(UTC)
        self.db.execute(
            text("""
                INSERT INTO dashboard_sessions
                    (session_id, account_id, created_at, last_active_at, expires_at)
                VALUES (:session_id, :account_id, :now, :now, :expires_at)
                """),
            {
                "session_id": _hash_token(raw_token),
                "account_id": account_id,
                "now": now,
                "expires_at": now + self.session_duration,
            },
        )
        self.db.commit()
        return raw_token

    def validate(self, raw_token: str) -> DashboardSession | None:
        """Returns None for a missing, expired, or unknown token —
        never raises, since "not logged in" is a routine outcome, not
        an error. Touches last_active_at on every valid use (the
        spec's "refreshed on activity")."""
        session_id = _hash_token(raw_token)
        now = datetime.now(UTC)
        row = (
            self.db.execute(
                text("""
                    SELECT session_id, account_id, created_at, last_active_at, expires_at
                    FROM dashboard_sessions
                    WHERE session_id = :session_id
                    """),
                {"session_id": session_id},
            )
            .mappings()
            .first()
        )
        if row is None or row["expires_at"] <= now:
            return None

        self.db.execute(
            text(
                "UPDATE dashboard_sessions SET last_active_at = :now WHERE session_id = :session_id"
            ),
            {"now": now, "session_id": session_id},
        )
        self.db.commit()
        return DashboardSession(
            session_id=row["session_id"],
            account_id=row["account_id"],
            created_at=row["created_at"],
            last_active_at=now,
            expires_at=row["expires_at"],
        )

    def revoke(self, raw_token: str) -> None:
        self.db.execute(
            text("DELETE FROM dashboard_sessions WHERE session_id = :session_id"),
            {"session_id": _hash_token(raw_token)},
        )
        self.db.commit()

    def purge_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        result = cast(
            CursorResult,
            self.db.execute(
                text("DELETE FROM dashboard_sessions WHERE expires_at <= :now"), {"now": now}
            ),
        )
        self.db.commit()
        return result.rowcount or 0
