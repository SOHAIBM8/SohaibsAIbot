"""
End-to-end integration test: a fake Anthropic client (no real network,
same pattern as test_llm_client.py) scripts a realistic two-turn
exchange — first response requests the get_trade tool, second response
uses the (real, account-scoped, Postgres-backed) tool result to produce
final text. Proves the full wiring: LLMClient -> ChatToolRegistry
(account-scoped, real llm_readonly-backed ContextBuilder) -> llm_query_log,
against real local Postgres.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.chat_query_service import ChatQueryService
from core.ai_assistant.chat_tool import GetTradeTool
from core.ai_assistant.chat_tool_registry import ChatToolRegistry
from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.llm_client import LLMClient
from core.ai_assistant.llm_usage_tracker import LLMUsageTracker
from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal

ACCOUNT_ID = "test_cqs_account"
OTHER_ACCOUNT_ID = "test_cqs_other_account"
ORDER_ID = "test_co_cqs_1"


@dataclass
class FakeTextBlock:
    type: str
    text: str


@dataclass
class FakeToolUseBlock:
    type: str
    id: str
    name: str
    input: dict


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
    def __init__(self, *args, pinned_date: date, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_date = pinned_date

    def _today(self) -> date:
        return self._pinned_date


@pytest.fixture
def readonly_db():
    session = ReadonlySessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def write_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM llm_query_log WHERE account_id LIKE 'test_cqs%'"))
        session.execute(text("DELETE FROM fills WHERE client_order_id = :o"), {"o": ORDER_ID})
        session.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": ORDER_ID})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = 'test_strategy_cqs'")
        )
        session.execute(text("DELETE FROM signal_log WHERE strategy_id = 'test_strategy_cqs'"))
        session.execute(text("DELETE FROM llm_usage_daily WHERE usage_date >= '2999-09-01'"))
        session.execute(text("DELETE FROM paper_accounts WHERE account_id LIKE 'test_cqs%'"))
        session.commit()
        session.close()


@pytest.fixture
def seeded_trade(write_db):
    write_db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 1000, 1000, now()), (:o, 1000, 1000, now())
            """),
        {"a": ACCOUNT_ID, "o": OTHER_ACCOUNT_ID},
    )
    write_db.execute(
        text("""
            INSERT INTO signal_log (symbol, bar_time, strategy_id, direction, regime)
            VALUES ('BTC/USDT', :t, 'test_strategy_cqs', 1, 'trending_up')
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC)},
    )
    decision_id = write_db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, 'test_strategy_cqs', 1.0, '[]')
            RETURNING id
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC)},
    ).scalar_one()
    write_db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                (:o, 'test_strategy_cqs', 'BTC/USDT', 'market', 1, 1.0,
                 'paper', 'filled', :decision_id, :t, :t, :a)
            """),
        {
            "o": ORDER_ID,
            "decision_id": decision_id,
            "t": datetime(2024, 6, 1, tzinfo=UTC),
            "a": ACCOUNT_ID,
        },
    )
    write_db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES (:o, 65000.0, 1.0, 5.0, FALSE, :t)
            """),
        {"o": ORDER_ID, "t": datetime(2024, 6, 1, tzinfo=UTC)},
    )
    write_db.commit()


def make_service(write_db, readonly_db, fake_anthropic, pinned_date):
    tracker = FrozenTracker(daily_cap_calls=10, db_session=write_db, pinned_date=pinned_date)
    llm_client = LLMClient(
        api_key_env_var="ANTHROPIC_API_KEY",
        model="claude-fake-model",
        usage_tracker=tracker,
        anthropic_client=fake_anthropic,
    )
    context_builder = ContextBuilder(readonly_db)
    registry = ChatToolRegistry([GetTradeTool(context_builder, readonly_db)])
    return ChatQueryService(llm_client, registry, write_db)


def test_answer_executes_a_tool_call_and_returns_grounded_text(seeded_trade, write_db, readonly_db):
    fake_anthropic = ScriptedAnthropicMessages(
        [
            FakeRawMessage(
                content=[
                    FakeToolUseBlock(
                        type="tool_use", id="call_1", name="get_trade", input={"order_id": ORDER_ID}
                    )
                ],
                usage=FakeUsage(input_tokens=50, output_tokens=10),
            ),
            FakeRawMessage(
                content=[
                    FakeTextBlock(
                        type="text",
                        text="You bought 1.0 BTC/USDT at 65000.0 during a trending_up regime.",
                    )
                ],
                usage=FakeUsage(input_tokens=80, output_tokens=20),
            ),
        ]
    )
    service = make_service(write_db, readonly_db, fake_anthropic, date(2999, 9, 1))

    answer = service.answer(account_id=ACCOUNT_ID, question="What happened with my last trade?")

    assert "65000.0" in answer
    assert len(fake_anthropic.calls) == 2  # one initial call, one follow-up

    row = (
        write_db.execute(
            text("""
                SELECT account_id, question, tool_calls_made, response
                FROM llm_query_log WHERE account_id = :a
                """),
            {"a": ACCOUNT_ID},
        )
        .mappings()
        .first()
    )
    assert row["account_id"] == ACCOUNT_ID
    assert row["tool_calls_made"] == ["get_trade"]
    assert "65000.0" in row["response"]


def test_answer_without_any_tool_call_still_logs(write_db, readonly_db):
    fake_anthropic = ScriptedAnthropicMessages(
        [
            FakeRawMessage(
                content=[FakeTextBlock(type="text", text="I don't have enough information.")],
                usage=FakeUsage(input_tokens=20, output_tokens=10),
            )
        ]
    )
    service = make_service(write_db, readonly_db, fake_anthropic, date(2999, 9, 2))

    answer = service.answer(account_id=ACCOUNT_ID, question="What's the weather?")

    assert answer == "I don't have enough information."
    assert len(fake_anthropic.calls) == 1  # no tool requested -> no follow-up call

    row = (
        write_db.execute(
            text("SELECT tool_calls_made FROM llm_query_log WHERE account_id = :a"),
            {"a": ACCOUNT_ID},
        )
        .mappings()
        .first()
    )
    assert row["tool_calls_made"] == []


def test_answer_never_leaks_another_accounts_trade_via_tool_use(
    seeded_trade, write_db, readonly_db
):
    """Even inside the full ChatQueryService pipeline (not just
    ChatToolRegistry directly), a scripted attempt to fetch another
    account's order must be refused."""
    fake_anthropic = ScriptedAnthropicMessages(
        [
            FakeRawMessage(
                content=[
                    FakeToolUseBlock(
                        type="tool_use", id="call_1", name="get_trade", input={"order_id": ORDER_ID}
                    )
                ],
                usage=FakeUsage(input_tokens=50, output_tokens=10),
            ),
            FakeRawMessage(
                content=[FakeTextBlock(type="text", text="No trade found for this account.")],
                usage=FakeUsage(input_tokens=30, output_tokens=10),
            ),
        ]
    )
    service = make_service(write_db, readonly_db, fake_anthropic, date(2999, 9, 3))

    # OTHER_ACCOUNT_ID's session asks about ORDER_ID, which belongs to ACCOUNT_ID.
    answer = service.answer(account_id=OTHER_ACCOUNT_ID, question="What happened with order X?")

    assert "65000.0" not in answer
    assert "No trade found" in answer
