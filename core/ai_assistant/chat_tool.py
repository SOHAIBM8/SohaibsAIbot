"""
ChatTool interface + concrete read-only tools. Deliberately no
write-capable tool exists in this file or anywhere in this component —
not "unused," structurally absent (decision #6).

Security note: `account_id` in execute() is ALWAYS the value
ChatToolRegistry injected from the authenticated session, never read
from LLM-supplied params (decision #2, enforced at the registry — see
chat_tool_registry.py). But injection resistance doesn't stop at the
registry boundary: a tool whose OWN params can name a different
account's resource (an order_id, say) would leak cross-account data
purely by ID-guessing, regardless of how correctly account_id itself
was injected. GetTradeTool therefore verifies the requested order
actually belongs to account_id before returning anything — every tool
here must carry that same discipline for any resource it looks up by a
caller-supplied id.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, time
from datetime import date as date_type

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ai_assistant.context_builder import ContextBuilder


class ChatTool(ABC):
    name: str
    description: str

    @abstractmethod
    def execute(self, account_id: str, **params: object) -> dict:
        """account_id is ALWAYS the value injected by ChatToolRegistry
        from the authenticated session — never read from params, even
        if the caller (including the LLM) supplies one."""


class GetTradeTool(ChatTool):
    name = "get_trade"
    description = "Look up one trade (order) belonging to the caller's account, by order_id."

    def __init__(self, context_builder: ContextBuilder, readonly_db: Session):
        self.context_builder = context_builder
        self.db = readonly_db

    def execute(self, account_id: str, order_id: str) -> dict:  # type: ignore[override]
        # Each concrete tool intentionally declares its own named
        # params instead of **params — that's the whole point of a
        # heterogeneous tool registry (every tool needs different
        # arguments). mypy sees this as a Liskov violation against the
        # base class's **params signature; it's deliberate, not a bug.
        owner = self.db.execute(
            text("SELECT account_id FROM orders WHERE client_order_id = :order_id"),
            {"order_id": order_id},
        ).scalar_one_or_none()
        if owner is None or owner != account_id:
            # Same response whether the order doesn't exist or belongs
            # to someone else — distinguishing the two would itself
            # leak that a given order_id exists on another account.
            return {"error": "no trade found for this account with that order_id"}

        context = self.context_builder.build_trade_context(order_id)
        return {
            "order_id": context.order.client_order_id,
            "symbol": context.order.symbol,
            "direction": context.order.direction,
            "quantity": context.order.quantity,
            "state": context.order.state.value,
            "fills": [
                {"fill_price": f.fill_price, "quantity": f.quantity, "fee": f.fee}
                for f in context.fills
            ],
            "regime_at_entry": context.regime_at_entry,
            "risk_decision_id": context.risk_decision.id,
        }


class GetRiskDecisionsTool(ChatTool):
    name = "get_risk_decisions"
    description = "List risk engine decisions for the caller's account on a given date."

    def __init__(self, context_builder: ContextBuilder):
        self.context_builder = context_builder

    def execute(self, account_id: str, date: str) -> dict:  # type: ignore[override]
        parsed_date = date_type.fromisoformat(date)
        day_start = datetime.combine(parsed_date, time.min, tzinfo=UTC)
        day_end = datetime.combine(parsed_date, time.max, tzinfo=UTC)
        decisions = self.context_builder.fetch_risk_decisions_for_account(
            account_id, day_start, day_end
        )
        return {
            "decisions": [
                {
                    "id": d.id,
                    "strategy_id": d.strategy_id,
                    "proposed_quantity": d.proposed_quantity,
                    "approved_quantity": d.approved_quantity,
                    "rejection_reason": d.rejection_reason,
                    "throttle_reasons": d.throttle_reasons,
                }
                for d in decisions
            ]
        }


class GetRegimeHistoryTool(ChatTool):
    name = "get_regime_history"
    description = "Look up recent market regime classifications for a symbol within a time window."

    def __init__(self, context_builder: ContextBuilder):
        self.context_builder = context_builder

    def execute(  # type: ignore[override]
        self, account_id: str, symbol: str, start: str, end: str
    ) -> dict:
        context = self.context_builder.build_regime_context(
            symbol, datetime.fromisoformat(start), datetime.fromisoformat(end)
        )
        return {"symbol": context.symbol, "regime_history": context.regime_history}


class SearchNewsTool(ChatTool):
    name = "search_news"
    description = "Search recently ingested crypto news articles by keyword."

    def __init__(self, readonly_db: Session):
        self.db = readonly_db

    def execute(self, account_id: str, keyword: str, limit: int = 5) -> dict:  # type: ignore[override]
        rows = (
            self.db.execute(
                text("""
                    SELECT title, url, source, published_at
                    FROM news_articles
                    WHERE title ILIKE :pattern OR raw_content ILIKE :pattern
                    ORDER BY published_at DESC NULLS LAST
                    LIMIT :limit
                    """),
                {"pattern": f"%{keyword}%", "limit": limit},
            )
            .mappings()
            .all()
        )
        return {"articles": [dict(row) for row in rows]}
