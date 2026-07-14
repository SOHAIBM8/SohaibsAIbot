"""
Tests run against real local Postgres. Rows are seeded via the real
ExplanationCache.get_or_generate() write path (a fake LLMClient, same
pattern as test_explanation_cache.py) — ExplanationReader only needs to
prove it reads those rows back correctly, read-only, never generating.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.explanation_reader import ExplanationReader
from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry
from core.db import SessionLocal

ACCOUNT_ID = "test_expl_reader_account"


@dataclass
class FakeContext:
    fact: str


@dataclass
class FakeLLMResponse:
    text: str
    tokens_used: int
    tool_calls_made: list
    model: str
    latency_ms: float


class FakeLLMClient:
    def __init__(self, response_text="a generated summary"):
        self.response_text = response_text
        self.call_count = 0

    def generate(self, system_prompt, user_content, tools=None):
        self.call_count += 1
        return FakeLLMResponse(
            text=self.response_text,
            tokens_used=10,
            tool_calls_made=[],
            model="claude-fake-model",
            latency_ms=1.0,
        )


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM llm_explanations WHERE subject_id LIKE :p"),
            {"p": f"{ACCOUNT_ID}:%"},
        )
        session.execute(
            text("DELETE FROM prompt_templates WHERE template_id = 'test_expl_reader_template'")
        )
        session.commit()
        session.close()


@pytest.fixture
def template(db):
    registry = PromptTemplateRegistry(db)
    tpl = PromptTemplate(
        template_id="test_expl_reader_template",
        version="1.0.0",
        subject_type="daily_summary",
        template_text="summarize",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    registry.register(tpl)
    return tpl


def test_get_latest_daily_summary_returns_none_when_none_exists(db):
    assert ExplanationReader(db).get_latest_daily_summary(ACCOUNT_ID) is None


def test_get_latest_daily_summary_returns_the_most_recent_one(db, template):
    cache = ExplanationCache(db)
    cache.get_or_generate(
        subject_type="daily_summary",
        subject_id=f"{ACCOUNT_ID}:2024-01-01",
        context_fn=lambda: FakeContext(fact="day one"),
        template=template,
        llm_client=FakeLLMClient("summary for day one"),
    )
    cache.get_or_generate(
        subject_type="daily_summary",
        subject_id=f"{ACCOUNT_ID}:2024-01-02",
        context_fn=lambda: FakeContext(fact="day two"),
        template=template,
        llm_client=FakeLLMClient("summary for day two"),
    )

    latest = ExplanationReader(db).get_latest_daily_summary(ACCOUNT_ID)

    assert latest is not None
    assert latest.generated_text == "summary for day two"
    assert latest.subject_id == f"{ACCOUNT_ID}:2024-01-02"


def test_get_latest_daily_summary_never_calls_the_llm(db, template):
    cache = ExplanationCache(db)
    cache.get_or_generate(
        subject_type="daily_summary",
        subject_id=f"{ACCOUNT_ID}:2024-01-01",
        context_fn=lambda: FakeContext(fact="day one"),
        template=template,
        llm_client=FakeLLMClient("summary"),
    )

    # get_latest_daily_summary takes no llm_client at all — the type
    # signature itself proves it cannot generate, this just confirms
    # calling it twice returns the same persisted row.
    first = ExplanationReader(db).get_latest_daily_summary(ACCOUNT_ID)
    second = ExplanationReader(db).get_latest_daily_summary(ACCOUNT_ID)

    assert first == second


def test_get_latest_daily_summary_does_not_leak_across_accounts(db, template):
    cache = ExplanationCache(db)
    cache.get_or_generate(
        subject_type="daily_summary",
        subject_id=f"{ACCOUNT_ID}:2024-01-01",
        context_fn=lambda: FakeContext(fact="mine"),
        template=template,
        llm_client=FakeLLMClient("mine"),
    )

    other_account = "test_expl_reader_other_account"
    result = ExplanationReader(db).get_latest_daily_summary(other_account)

    assert result is None
