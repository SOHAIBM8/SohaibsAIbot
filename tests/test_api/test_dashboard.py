"""
Dashboard overview API integration tests against real local Postgres.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

ACCOUNT_ID = "test_dashboard_account"
STRATEGY_ID = "test_dashboard_overview_strategy"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return client


def test_overview_requires_auth(client):
    response = client.get("/api/dashboard/overview")
    assert response.status_code == 401


def test_overview_returns_gap_stubs_when_nothing_seeded(client, db):
    _logged_in(client)

    response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"]["available"] is False
    assert body["open_position_count"]["available"] is False
    assert body["today_pnl"]["available"] is False
    assert body["equity_curve"]["available"] is False
    assert body["latest_daily_summary"] is None


@pytest.fixture
def seeded_risk_decision(db):
    db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, 0.5, :layer_results)
            """),
        {
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "s": STRATEGY_ID,
            "layer_results": json.dumps(
                [{"layer_name": "budget", "passed": True, "multiplier": 0.5, "reason": "tier 1"}]
            ),
        },
    )
    db.commit()
    yield
    db.execute(text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
    db.commit()


def test_overview_includes_recent_risk_decisions(client, db, seeded_risk_decision):
    _logged_in(client)

    response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    strategy_ids = [d["strategy_id"] for d in response.json()["recent_risk_decisions"]]
    assert STRATEGY_ID in strategy_ids


@pytest.fixture
def seeded_daily_summary(db):
    registry = PromptTemplateRegistry(db)
    template = PromptTemplate(
        template_id="test_dashboard_overview_template",
        version="1.0.0",
        subject_type="daily_summary",
        template_text="summarize",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    registry.register(template)

    class FakeResponse:
        text = "a great trading day"
        tokens_used = 10
        tool_calls_made: list = []
        model = "claude-fake-model"
        latency_ms = 1.0

    class FakeLLMClient:
        def generate(self, system_prompt, user_content, tools=None):
            return FakeResponse()

    cache = ExplanationCache(db)
    cache.get_or_generate(
        subject_type="daily_summary",
        subject_id=f"{ACCOUNT_ID}:2024-01-01",
        context_fn=lambda: template,  # any dataclass instance works for the hash
        template=template,
        llm_client=FakeLLMClient(),
    )
    db.commit()
    yield
    db.execute(
        text("DELETE FROM llm_explanations WHERE subject_id LIKE :p"), {"p": f"{ACCOUNT_ID}:%"}
    )
    db.execute(
        text("DELETE FROM prompt_templates WHERE template_id = :t"), {"t": template.template_id}
    )
    db.commit()


def test_overview_includes_latest_daily_summary_when_present(client, db, seeded_daily_summary):
    _logged_in(client)

    response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    summary = response.json()["latest_daily_summary"]
    assert summary is not None
    assert summary["generated_text"] == "a great trading day"
