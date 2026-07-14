"""
Read-only "has an explanation already been generated" access —
distinct from ExplanationCache, which only ever does exact-match-or-
generate (get_or_generate). The dashboard overview page (spec section
8: "the latest AI-generated daily summary if one exists") needs a
NEVER-generates read: does a daily_summary explanation already exist
for this account, and if so, the most recent one. No such method
exists anywhere in core/ai_assistant/ today (confirmed by direct
search) — ExplanationCache's own docstring frames it as a
generate-oriented class, so this stays a separate, purely-query class
rather than bolting a read-only concern onto it.

Takes a normal core.db.SessionLocal session, NOT the llm_readonly one
— schema.sql's `GRANT SELECT ... TO llm_readonly` list never included
`llm_explanations` (only signal_log/risk_decision_log/orders/fills/
experiments/paper_accounts/account_snapshots/news_articles), matching
ExplanationCache itself, which always reads/writes llm_explanations
via the writable session it's constructed with, never the readonly
one. Confirmed empirically: querying llm_explanations as llm_readonly
raises InsufficientPrivilege.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class ExplanationSummary:
    explanation_id: int
    subject_type: str
    subject_id: str
    generated_text: str
    generated_at: datetime


class ExplanationReader:
    def __init__(self, db: Session):
        self.db = db

    def get_latest_daily_summary(self, account_id: str) -> ExplanationSummary | None:
        """subject_id for a daily summary is f"{account_id}:{date.isoformat()}"
        (core/ai_assistant/daily_summary_job.py) — the LIKE prefix match
        below is the same convention, applied read-only."""
        row = (
            self.db.execute(
                text("""
                    SELECT explanation_id, subject_type, subject_id, generated_text, generated_at
                    FROM llm_explanations
                    WHERE subject_type = 'daily_summary' AND subject_id LIKE :prefix
                    ORDER BY generated_at DESC
                    LIMIT 1
                    """),
                {"prefix": f"{account_id}:%"},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return ExplanationSummary(**row)
