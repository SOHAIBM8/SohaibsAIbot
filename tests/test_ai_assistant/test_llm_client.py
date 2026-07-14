"""
Fake LLM responses only, no real network call — exactly like every
other "no real network in unit tests" component in this project
(BinanceAdapter/FakeExchangeAdapter, WebSocketConnection/fake_server).
Usage tracking (LLMUsageTracker) is real, against real local Postgres.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.ai_assistant.llm_client import LLMClient, LLMUsageCapExceededError
from core.ai_assistant.llm_usage_tracker import LLMUsageTracker
from core.db import SessionLocal


@dataclass
class FakeTextBlock:
    type: str
    text: str


@dataclass
class FakeToolUseBlock:
    type: str
    name: str


@dataclass
class FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class FakeRawMessage:
    content: list
    usage: FakeUsage
    model: str = "claude-fake-model"


class FakeAnthropicMessages:
    """Records every call it receives and returns a pre-scripted
    response, in call order — a test sets .responses up front."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FrozenTracker(LLMUsageTracker):
    def __init__(self, *args, pinned_date: date, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_date = pinned_date

    def _today(self) -> date:
        return self._pinned_date


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM llm_usage_daily WHERE usage_date >= '2999-06-01'"))
        session.commit()
        session.close()


def fixed_date(offset_days: int = 0) -> date:
    return date(2999, 6, 1) + timedelta(days=offset_days)


def test_generate_returns_parsed_text_and_token_usage(db):
    fake_client = FakeAnthropicMessages(
        [
            FakeRawMessage(
                content=[
                    FakeTextBlock(type="text", text="This trade entered on a bullish EMA cross.")
                ],
                usage=FakeUsage(input_tokens=120, output_tokens=40),
            )
        ]
    )
    tracker = FrozenTracker(daily_cap_calls=10, db_session=db, pinned_date=fixed_date(0))
    client = LLMClient(
        api_key_env_var="ANTHROPIC_API_KEY",
        model="claude-fake-model",
        usage_tracker=tracker,
        anthropic_client=fake_client,
    )

    response = client.generate(system_prompt="Explain the trade.", user_content='{"facts": true}')

    assert response.text == "This trade entered on a bullish EMA cross."
    assert response.tokens_used == 160
    assert response.tool_calls_made == []
    assert response.model == "claude-fake-model"
    assert response.latency_ms >= 0


def test_generate_records_usage_against_the_tracker(db):
    fake_client = FakeAnthropicMessages(
        [
            FakeRawMessage(
                content=[FakeTextBlock(type="text", text="ok")],
                usage=FakeUsage(input_tokens=10, output_tokens=5),
            )
        ]
    )
    tracker = FrozenTracker(daily_cap_calls=10, db_session=db, pinned_date=fixed_date(1))
    client = LLMClient(
        api_key_env_var="ANTHROPIC_API_KEY",
        model="claude-fake-model",
        usage_tracker=tracker,
        anthropic_client=fake_client,
    )

    client.generate(system_prompt="sp", user_content="uc")

    snapshot = tracker.snapshot()
    assert snapshot.calls_made == 1
    assert snapshot.tokens_used == 15
    assert snapshot.estimated_cost > 0


def test_generate_refuses_once_the_daily_cap_is_reached(db):
    fake_client = FakeAnthropicMessages(
        [
            FakeRawMessage(
                content=[FakeTextBlock(type="text", text="first")],
                usage=FakeUsage(input_tokens=10, output_tokens=5),
            )
        ]
    )
    tracker = FrozenTracker(daily_cap_calls=1, db_session=db, pinned_date=fixed_date(2))
    client = LLMClient(
        api_key_env_var="ANTHROPIC_API_KEY",
        model="claude-fake-model",
        usage_tracker=tracker,
        anthropic_client=fake_client,
    )

    client.generate(system_prompt="sp", user_content="uc")  # consumes the cap

    with pytest.raises(LLMUsageCapExceededError):
        client.generate(system_prompt="sp", user_content="uc again")

    # the refused call never reached the fake client
    assert len(fake_client.calls) == 1


def test_generate_parses_tool_use_blocks_into_tool_calls_made(db):
    fake_client = FakeAnthropicMessages(
        [
            FakeRawMessage(
                content=[
                    FakeTextBlock(type="text", text="Looking it up."),
                    FakeToolUseBlock(type="tool_use", name="get_trade"),
                ],
                usage=FakeUsage(input_tokens=50, output_tokens=10),
            )
        ]
    )
    tracker = FrozenTracker(daily_cap_calls=10, db_session=db, pinned_date=fixed_date(3))
    client = LLMClient(
        api_key_env_var="ANTHROPIC_API_KEY",
        model="claude-fake-model",
        usage_tracker=tracker,
        anthropic_client=fake_client,
    )

    response = client.generate(system_prompt="sp", user_content="uc")

    assert response.text == "Looking it up."
    assert response.tool_calls_made == ["get_trade"]


def test_estimate_cost_is_proportional_to_tokens():
    assert LLMClient.estimate_cost(1000) == pytest.approx(2 * LLMClient.estimate_cost(500))
    assert LLMClient.estimate_cost(0) == 0
