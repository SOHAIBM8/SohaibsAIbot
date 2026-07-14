"""
Resolves which account_id an EventBus event belongs to, so the gateway
can scope delivery correctly (spec section 19/23: "never a firehose of
every system-wide event to every connected client"). Most Stage 1/2
execution events (`OrderSubmitted`, `OrderFilled`, ...) don't carry
account_id directly — only `client_order_id` — so this resolver falls
back to a real lookup against `orders.account_id`.

Deliberately conservative: an event that can't be attributed to a
specific account is DROPPED, never broadcast to everyone. Sending
account-scoped data to the wrong (or every) connection would be a real
security regression, not just a display bug.
"""

from collections.abc import Callable

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


class OrderAccountResolver:
    """Takes a session FACTORY (e.g. `core.db.SessionLocal`), not a
    bound Session — this resolver is called repeatedly over the
    lifetime of a long-running background gateway process, so it opens
    and closes a short-lived session per lookup rather than holding one
    connection open indefinitely."""

    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def resolve(self, event_payload: dict) -> str | None:
        if "account_id" in event_payload and event_payload["account_id"]:
            return str(event_payload["account_id"])

        client_order_id = event_payload.get("client_order_id")
        if not client_order_id:
            return None

        db = self.session_factory()
        try:
            row = (
                db.execute(
                    text("SELECT account_id FROM orders WHERE client_order_id = :client_order_id"),
                    {"client_order_id": client_order_id},
                )
                .mappings()
                .first()
            )
        finally:
            db.close()

        if row is None or row["account_id"] is None:
            logger.warning("websocket_event_account_unresolved", client_order_id=client_order_id)
            return None
        return str(row["account_id"])
