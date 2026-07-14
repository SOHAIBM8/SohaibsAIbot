"""
AI Assistant API integration tests against real local Postgres. Uses
FastAPI's dependency_overrides to swap the real (lazy, network-backed)
LLMClient for one built with a scripted fake Anthropic client — same
fake pattern as
tests/test_ai_assistant/test_chat_query_service_integration.py, never
real network/API key.
"""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from api.ai_assistant_deps import get_llm_client
from api.ai_assistant_templates import ensure_templates_registered
from core.ai_assistant.llm_client import LLMClient
from core.ai_assistant.llm_usage_tracker import LLMUsageTracker
from core.db import SessionLocal
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

ACCOUNT_ID = "test_dashboard_account"
OTHER_ACCOUNT_ID = "test_ai_other_account"
STRATEGY_ID = "test_ai_assistant_strategy"
ORDER_ID = "test_ai_co_1"


@dataclass
class FakeTextBlock:
    type: str
    text: str


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class FakeRawMessage:
    content: list
    usage: FakeUsage
    model: str = "claude-fake-model"


class ScriptedAnthropicMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FrozenTracker(LLMUsageTracker):
    """Pins _today() to a far-future, test-unique date so this file's
    llm_usage_daily writes never collide with the real calendar date
    (which would otherwise leak daily_cap_reached=TRUE across runs,
    since that row persists in real Postgres) or with each other."""

    def __init__(self, *args, pinned_date: date, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_date = pinned_date

    def _today(self) -> date:
        return self._pinned_date


_pinned_dates = (date(2999, 1, 1) + timedelta(days=i) for i in itertools.count())


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return response.cookies["dashboard_csrf"]


def _override_llm_client(app, response_text: str, daily_cap_calls: int = 10):
    def _get() -> LLMClient:
        fake = ScriptedAnthropicMessages(
            [
                FakeRawMessage(
                    content=[FakeTextBlock(type="text", text=response_text)],
                    usage=FakeUsage(input_tokens=10, output_tokens=5),
                )
            ]
        )
        tracker = FrozenTracker(
            daily_cap_calls=daily_cap_calls,
            db_session=SessionLocal(),
            pinned_date=next(_pinned_dates),
        )
        return LLMClient(
            api_key_env_var="ANTHROPIC_API_KEY",
            model="claude-fake-model",
            usage_tracker=tracker,
            anthropic_client=fake,
        )

    app.dependency_overrides[get_llm_client] = _get


@pytest.fixture
def app_with_templates(db):
    ensure_templates_registered(db)
    from api.main import app

    yield app
    app.dependency_overrides.clear()
    db.execute(text("DELETE FROM llm_usage_daily WHERE usage_date >= '2999-01-01'"))
    db.execute(text("DELETE FROM llm_query_log WHERE account_id = :a"), {"a": ACCOUNT_ID})
    db.commit()


@pytest.fixture
def seeded_order(db):
    db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 100000, 100000, :t)
            ON CONFLICT (account_id) DO NOTHING
            """),
        {"a": ACCOUNT_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.execute(
        text("""
            INSERT INTO signal_log (symbol, bar_time, strategy_id, direction, regime)
            VALUES ('BTC/USDT', :t, :s, 1, 'trending_up')
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    )
    decision_id = db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, :s, 1.0, '[]') RETURNING id
            """),
        {"t": datetime(2024, 1, 1, tzinfo=UTC), "s": STRATEGY_ID},
    ).scalar_one()
    db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                (:o, :s, 'BTC/USDT', 'market', 1, 1.0, 'paper', 'filled', :d, :t, :t, :a)
            """),
        {
            "o": ORDER_ID,
            "s": STRATEGY_ID,
            "d": decision_id,
            "t": datetime(2024, 1, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES (:o, 100.0, 1.0, 0.1, FALSE, :t)
            """),
        {"o": ORDER_ID, "t": datetime(2024, 1, 1, tzinfo=UTC)},
    )
    db.commit()
    yield decision_id
    db.execute(text("DELETE FROM llm_explanations WHERE subject_id = :o"), {"o": ORDER_ID})
    db.execute(text("DELETE FROM llm_explanations WHERE subject_id = :d"), {"d": str(decision_id)})
    db.execute(text("DELETE FROM fills WHERE client_order_id = :o"), {"o": ORDER_ID})
    db.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": ORDER_ID})
    db.execute(text("DELETE FROM risk_decision_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
    db.execute(text("DELETE FROM signal_log WHERE strategy_id = :s"), {"s": STRATEGY_ID})
    db.commit()


def test_chat_requires_auth(client):
    response = client.post("/api/ai/chat", json={"question": "hi"})
    assert response.status_code == 401


def test_chat_returns_scripted_answer(app_with_templates, client):
    _override_llm_client(app_with_templates, "This is a scripted answer.")
    csrf_token = _logged_in(client)

    response = client.post(
        "/api/ai/chat",
        json={"question": "What happened today?"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "This is a scripted answer."


def test_chat_maps_usage_cap_exceeded_to_429(app_with_templates, client):
    _override_llm_client(app_with_templates, "unused", daily_cap_calls=0)
    csrf_token = _logged_in(client)

    response = client.post(
        "/api/ai/chat", json={"question": "hi"}, headers={"X-CSRF-Token": csrf_token}
    )

    assert response.status_code == 429


def test_explain_trade_requires_auth(client):
    response = client.get(f"/api/ai/explanations/trade/{ORDER_ID}")
    assert response.status_code == 401


def test_explain_trade_returns_404_for_unknown_order(app_with_templates, client):
    _logged_in(client)
    response = client.get("/api/ai/explanations/trade/no-such-order")
    assert response.status_code == 404


def test_explain_trade_generates_and_returns_explanation(app_with_templates, client, seeded_order):
    _override_llm_client(app_with_templates, "You bought 1.0 BTC/USDT at 100.0.")
    _logged_in(client)

    response = client.get(f"/api/ai/explanations/trade/{ORDER_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["subject_type"] == "trade"
    assert body["subject_id"] == ORDER_ID
    assert "100.0" in body["generated_text"]


def test_explain_trade_second_call_is_a_cache_hit(app_with_templates, client, seeded_order):
    _override_llm_client(app_with_templates, "First generation.")
    _logged_in(client)
    first = client.get(f"/api/ai/explanations/trade/{ORDER_ID}")
    assert first.status_code == 200

    # Swap in a fake with zero scripted responses — if this call reaches
    # the LLM at all (a cache miss), popping an empty list raises.
    def _get_empty() -> LLMClient:
        return LLMClient(
            api_key_env_var="ANTHROPIC_API_KEY",
            model="claude-fake-model",
            usage_tracker=FrozenTracker(
                daily_cap_calls=10, db_session=SessionLocal(), pinned_date=next(_pinned_dates)
            ),
            anthropic_client=ScriptedAnthropicMessages([]),
        )

    app_with_templates.dependency_overrides[get_llm_client] = _get_empty

    second = client.get(f"/api/ai/explanations/trade/{ORDER_ID}")

    assert second.status_code == 200
    assert second.json()["generated_text"] == "First generation."


def test_explain_risk_decision_generates_explanation(app_with_templates, client, seeded_order):
    decision_id = seeded_order
    _override_llm_client(app_with_templates, "The budget layer approved the full size.")
    _logged_in(client)

    response = client.get(f"/api/ai/explanations/risk-decision/{decision_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["subject_type"] == "risk_decision"
    assert body["subject_id"] == str(decision_id)


def test_explain_risk_decision_unknown_id_is_404(app_with_templates, client):
    _logged_in(client)
    response = client.get("/api/ai/explanations/risk-decision/999999999")
    assert response.status_code == 404


def test_daily_summary_404_when_no_snapshot_coverage(app_with_templates, client):
    _logged_in(client)
    response = client.get("/api/ai/daily-summary/2024-01-01")
    assert response.status_code == 404
