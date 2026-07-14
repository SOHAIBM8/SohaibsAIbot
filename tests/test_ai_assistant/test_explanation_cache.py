"""
Tests run against real local Postgres. A fake LLMClient (matching
test_llm_client.py's pattern) proves the cache never calls it on a hit
and always calls it on a miss.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry
from core.db import SessionLocal


@dataclass
class FakeSubjectContext:
    """A minimal stand-in dataclass — ExplanationCache only needs
    something that dataclasses.asdict() can serialize; it doesn't care
    which real context type (TradeExplanationContext, etc.) it is."""

    subject_name: str
    fact: float


@dataclass
class FakeLLMResponse:
    text: str
    tokens_used: int
    tool_calls_made: list
    model: str
    latency_ms: float


class FakeLLMClient:
    def __init__(self, response_text="a generated explanation"):
        self.call_count = 0
        self.response_text = response_text

    def generate(self, system_prompt, user_content, tools=None):
        self.call_count += 1
        return FakeLLMResponse(
            text=self.response_text,
            tokens_used=42,
            tool_calls_made=[],
            model="claude-fake-model",
            latency_ms=1.0,
        )


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM llm_explanations WHERE subject_id LIKE 'test_%'"))
        session.execute(text("DELETE FROM prompt_templates WHERE template_id LIKE 'test_%'"))
        session.commit()
        session.close()


@pytest.fixture
def template(db) -> PromptTemplate:
    t = PromptTemplate(
        template_id="test_cache_template",
        version="1.0.0",
        subject_type="trade",
        template_text="Explain this trade using only the given facts.",
        created_at=datetime.now(UTC),
    )
    PromptTemplateRegistry(db).register(t)
    return t


def test_cache_miss_calls_the_llm_and_persists(db, template):
    cache = ExplanationCache(db)
    llm_client = FakeLLMClient()
    context = FakeSubjectContext(subject_name="trade-1", fact=1.0)

    explanation = cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_1",
        context_fn=lambda: context,
        template=template,
        llm_client=llm_client,
    )

    assert llm_client.call_count == 1
    assert explanation.generated_text == "a generated explanation"
    assert explanation.prompt_version == "1.0.0"
    assert isinstance(explanation.explanation_id, int)


def test_identical_context_hash_is_a_cache_hit_no_second_llm_call(db, template):
    cache = ExplanationCache(db)
    llm_client = FakeLLMClient()
    context = FakeSubjectContext(subject_name="trade-2", fact=2.0)

    first = cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_2",
        context_fn=lambda: context,
        template=template,
        llm_client=llm_client,
    )
    second = cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_2",
        context_fn=lambda: FakeSubjectContext(
            subject_name="trade-2", fact=2.0
        ),  # same facts, new object
        template=template,
        llm_client=llm_client,
    )

    assert llm_client.call_count == 1  # only the first call reached the LLM
    assert second.explanation_id == first.explanation_id


def test_changed_context_is_a_cache_miss_and_regenerates(db, template):
    cache = ExplanationCache(db)
    llm_client = FakeLLMClient()

    first = cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_3",
        context_fn=lambda: FakeSubjectContext(subject_name="trade-3", fact=1.0),
        template=template,
        llm_client=llm_client,
    )
    second = cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_3",
        context_fn=lambda: FakeSubjectContext(subject_name="trade-3", fact=2.0),  # changed fact
        template=template,
        llm_client=llm_client,
    )

    assert llm_client.call_count == 2
    assert second.explanation_id != first.explanation_id


def test_cache_miss_publishes_explanation_generated(db, template):
    event_bus = FakeEventBus()
    cache = ExplanationCache(db, event_bus=event_bus)
    llm_client = FakeLLMClient()

    cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_4",
        context_fn=lambda: FakeSubjectContext(subject_name="trade-4", fact=1.0),
        template=template,
        llm_client=llm_client,
    )

    assert len(event_bus.published) == 1
    assert event_bus.published[0].subject_id == "test_subject_4"


def test_cache_hit_does_not_republish(db, template):
    event_bus = FakeEventBus()
    cache = ExplanationCache(db, event_bus=event_bus)
    llm_client = FakeLLMClient()
    context = FakeSubjectContext(subject_name="trade-5", fact=1.0)

    cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_5",
        context_fn=lambda: context,
        template=template,
        llm_client=llm_client,
    )
    cache.get_or_generate(
        subject_type="trade",
        subject_id="test_subject_5",
        context_fn=lambda: context,
        template=template,
        llm_client=llm_client,
    )

    assert len(event_bus.published) == 1
