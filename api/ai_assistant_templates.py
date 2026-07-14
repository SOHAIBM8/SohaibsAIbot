"""
The dashboard's own well-known prompt templates, registered through
PromptTemplateRegistry — the ONLY place prompt wording is allowed to
live (docs/ai_assistant_spec.md decision #5; core/ai_assistant/
prompt_template.py's own docstring). PromptTemplateRegistry.get()
raises rather than falling back to an in-code default for an
unregistered id, and nothing else in this codebase seeds production
templates (only tests register ad hoc ones) — so the API layer must
register its own set once, idempotently, via the real registry write
path (register()'s ON CONFLICT DO UPDATE), exactly like RiskEngine
calls upsert_risk_config() on construction to guarantee its own FK is
always satisfiable. Called once from api/main.py's lifespan.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry

TRADE_TEMPLATE_ID = "dashboard_trade_explanation_v1"
RISK_DECISION_TEMPLATE_ID = "dashboard_risk_decision_explanation_v1"
DAILY_SUMMARY_TEMPLATE_ID = "dashboard_daily_summary_v1"

_TEMPLATES = [
    PromptTemplate(
        template_id=TRADE_TEMPLATE_ID,
        version="1.0.0",
        subject_type="trade",
        template_text=(
            "You are explaining one completed trade to the account owner. You will be given "
            "the order, its fills, the risk decision that approved it, and the market regime "
            "at entry — all as structured JSON facts. Explain in plain language what happened "
            "and why the risk engine sized it the way it did. Never invent a fact not present "
            "in the provided JSON."
        ),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ),
    PromptTemplate(
        template_id=RISK_DECISION_TEMPLATE_ID,
        version="1.0.0",
        subject_type="risk_decision",
        template_text=(
            "You are explaining one risk engine sizing decision to the account owner. You "
            "will be given the decision's layer-by-layer results as structured JSON facts. "
            "Explain which layer(s) reduced or rejected the trade and why, in plain language. "
            "Never invent a fact not present in the provided JSON."
        ),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ),
    PromptTemplate(
        template_id=DAILY_SUMMARY_TEMPLATE_ID,
        version="1.0.0",
        subject_type="daily_summary",
        template_text=(
            "You are summarizing one trading day for the account owner. You will be given "
            "the day's starting/ending equity and filled trades as structured JSON facts. "
            "Write a brief, plain-language summary of the day's activity and outcome. Never "
            "invent a fact not present in the provided JSON."
        ),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    ),
]


def ensure_templates_registered(db: Session) -> None:
    registry = PromptTemplateRegistry(db)
    for template in _TEMPLATES:
        registry.register(template)
