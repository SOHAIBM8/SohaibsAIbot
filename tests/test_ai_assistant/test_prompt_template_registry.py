"""
Tests run against real local Postgres, not mocks — consistent with
every other DB-touching component in this project. Each test cleans up
the prompt_templates row(s) it creates.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry
from core.db import SessionLocal


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM prompt_templates WHERE template_id LIKE 'test_%'"))
        session.commit()
        session.close()


def make_template(template_id="test_trade_v1", subject_type="trade") -> PromptTemplate:
    return PromptTemplate(
        template_id=template_id,
        version="1.0.0",
        subject_type=subject_type,
        template_text="Explain why this trade happened, using only the given facts.",
        created_at=datetime.now(UTC),
    )


def test_get_raises_for_unregistered_template_id(db):
    registry = PromptTemplateRegistry(db)
    with pytest.raises(KeyError, match="no prompt template registered"):
        registry.get("test_nonexistent")


def test_register_then_get_round_trips(db):
    registry = PromptTemplateRegistry(db)
    template = make_template()
    registry.register(template)

    fetched = registry.get("test_trade_v1")
    assert fetched.template_id == "test_trade_v1"
    assert fetched.version == "1.0.0"
    assert fetched.subject_type == "trade"
    assert fetched.template_text == template.template_text


def test_register_upserts_on_conflicting_template_id(db):
    registry = PromptTemplateRegistry(db)
    registry.register(make_template())

    updated = make_template()
    updated.version = "1.1.0"
    updated.template_text = "Updated wording."
    registry.register(updated)

    fetched = registry.get("test_trade_v1")
    assert fetched.version == "1.1.0"
    assert fetched.template_text == "Updated wording."


def test_different_subject_types_are_independent_templates(db):
    registry = PromptTemplateRegistry(db)
    registry.register(make_template(template_id="test_trade_v1", subject_type="trade"))
    registry.register(
        make_template(template_id="test_risk_decision_v1", subject_type="risk_decision")
    )

    trade_template = registry.get("test_trade_v1")
    risk_template = registry.get("test_risk_decision_v1")
    assert trade_template.subject_type == "trade"
    assert risk_template.subject_type == "risk_decision"
