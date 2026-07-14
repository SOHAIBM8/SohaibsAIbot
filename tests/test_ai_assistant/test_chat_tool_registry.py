"""
Structural test enumerating every registered tool and asserting none of
their names/descriptions imply a mutation — a hard test failure, not a
lint warning, if a future tool named e.g. `cancel_order` or
`update_risk_config` is ever added to this component (docs/ai_assistant_spec.md
section 5). Also proves execute_tool_call()'s account_id-injection and
unknown-tool behavior directly, against real local Postgres via the
llm_readonly role — the same connection every ChatTool actually uses.
"""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.chat_tool import (
    GetRegimeHistoryTool,
    GetRiskDecisionsTool,
    GetTradeTool,
    SearchNewsTool,
)
from core.ai_assistant.chat_tool_registry import ChatToolRegistry, UnknownToolError
from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal

# Any tool name or description matching one of these verbs is a hard
# failure — this is the check that would catch a future contributor
# accidentally (or maliciously) adding a mutating tool.
_MUTATION_VERB_PATTERN = re.compile(
    r"\b(place|cancel|update|delete|set|create|modify|write|insert|remove)\b", re.IGNORECASE
)

ACCOUNT_A = "test_ctr_account_a"
ACCOUNT_B = "test_ctr_account_b"
ORDER_A = "test_co_ctr_a"


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
        session.execute(text("DELETE FROM fills WHERE client_order_id = :o"), {"o": ORDER_A})
        session.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": ORDER_A})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = 'test_strategy_ctr'")
        )
        session.execute(text("DELETE FROM signal_log WHERE strategy_id = 'test_strategy_ctr'"))
        session.execute(
            text("DELETE FROM paper_accounts WHERE account_id IN (:a, :b)"),
            {"a": ACCOUNT_A, "b": ACCOUNT_B},
        )
        session.commit()
        session.close()


@pytest.fixture
def registry(readonly_db) -> ChatToolRegistry:
    context_builder = ContextBuilder(readonly_db)
    return ChatToolRegistry(
        [
            GetTradeTool(context_builder, readonly_db),
            GetRiskDecisionsTool(context_builder),
            GetRegimeHistoryTool(context_builder),
            SearchNewsTool(readonly_db),
        ]
    )


@pytest.fixture
def seeded_order(write_db):
    write_db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 1000, 1000, now()), (:b, 1000, 1000, now())
            """),
        {"a": ACCOUNT_A, "b": ACCOUNT_B},
    )
    write_db.execute(
        text("""
            INSERT INTO signal_log (symbol, bar_time, strategy_id, direction)
            VALUES ('BTC/USDT', :t, 'test_strategy_ctr', 1)
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC)},
    )
    decision_id = write_db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, 'test_strategy_ctr', 1.0, '[]')
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
                (:o, 'test_strategy_ctr', 'BTC/USDT', 'market', 1, 1.0,
                 'paper', 'filled', :decision_id, :t, :t, :a)
            """),
        {
            "o": ORDER_A,
            "decision_id": decision_id,
            "t": datetime(2024, 6, 1, tzinfo=UTC),
            "a": ACCOUNT_A,
        },
    )
    write_db.commit()
    return ORDER_A


def test_no_registered_tool_has_a_mutating_name_or_description(registry):
    for tool in registry.get_all():
        assert not _MUTATION_VERB_PATTERN.search(
            tool.name
        ), f"tool name '{tool.name}' looks mutating — ChatTool must be strictly read-only"
        assert not _MUTATION_VERB_PATTERN.search(
            tool.description
        ), f"tool '{tool.name}' description implies a mutation: {tool.description!r}"


def test_unknown_tool_name_is_rejected_not_silently_ignored(registry):
    with pytest.raises(UnknownToolError, match="unknown tool"):
        registry.execute_tool_call(
            account_id=ACCOUNT_A, tool_name="cancel_order", llm_supplied_params={}
        )


def test_account_id_supplied_in_params_is_discarded_not_honored(registry, seeded_order):
    # An LLM-supplied 'account_id' in params must never override the
    # real session account_id — this is decision #2's exact guarantee.
    result = registry.execute_tool_call(
        account_id=ACCOUNT_A,
        tool_name="get_trade",
        llm_supplied_params={"order_id": ORDER_A, "account_id": ACCOUNT_B},
    )
    assert "error" not in result
    assert result["order_id"] == ORDER_A


def test_get_trade_tool_refuses_another_accounts_order(registry, seeded_order):
    result = registry.execute_tool_call(
        account_id=ACCOUNT_B, tool_name="get_trade", llm_supplied_params={"order_id": ORDER_A}
    )
    assert "error" in result
    assert "order_id" not in result
