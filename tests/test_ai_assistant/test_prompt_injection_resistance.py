"""
The other acceptance-bar test for this component (alongside
test_readonly_role_enforcement.py) per spec section 7's Definition of
Done. A scripted fake LLM response — shaped exactly like a real
Anthropic tool_use block, the way LLMClient._parse() actually consumes
it — attempts two attacks in one scripted turn:

1. Ask for another account's data by supplying a different account_id
   inside the tool call's own input.
2. Ask for a tool that doesn't exist, one that would place/cancel/
   modify something if the LLM ever tried.

Both must be refused. Neither ever reaches real trading data belonging
to another account, and no unknown tool is silently no-op'd or
guessed at — it's a hard rejection.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.chat_tool import GetTradeTool
from core.ai_assistant.chat_tool_registry import ChatToolRegistry, UnknownToolError
from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal

VICTIM_ACCOUNT = "test_inj_victim"
ATTACKER_ACCOUNT = "test_inj_attacker"
VICTIM_ORDER = "test_co_inj_victim"


@dataclass
class ScriptedToolUseBlock:
    """Shaped like the real Anthropic SDK's tool_use content block —
    what LLMClient._parse() actually reads (block.name / an .input
    dict on the real SDK type). A crafted/compromised or merely
    confused LLM controls exactly this: the tool name and its input
    dict, nothing else — it never gets to choose account_id directly,
    only to try smuggling one inside `input`."""

    name: str
    input: dict


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
        session.execute(text("DELETE FROM fills WHERE client_order_id = :o"), {"o": VICTIM_ORDER})
        session.execute(text("DELETE FROM orders WHERE client_order_id = :o"), {"o": VICTIM_ORDER})
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = 'test_strategy_inj'")
        )
        session.execute(text("DELETE FROM signal_log WHERE strategy_id = 'test_strategy_inj'"))
        session.execute(
            text("DELETE FROM paper_accounts WHERE account_id IN (:v, :a)"),
            {"v": VICTIM_ACCOUNT, "a": ATTACKER_ACCOUNT},
        )
        session.commit()
        session.close()


@pytest.fixture
def victim_trade(write_db):
    write_db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:v, 1000, 1000, now()), (:a, 1000, 1000, now())
            """),
        {"v": VICTIM_ACCOUNT, "a": ATTACKER_ACCOUNT},
    )
    write_db.execute(
        text("""
            INSERT INTO signal_log (symbol, bar_time, strategy_id, direction)
            VALUES ('BTC/USDT', :t, 'test_strategy_inj', 1)
            """),
        {"t": datetime(2024, 6, 1, tzinfo=UTC)},
    )
    decision_id = write_db.execute(
        text("""
            INSERT INTO risk_decision_log (bar_time, strategy_id, approved_quantity, layer_results)
            VALUES (:t, 'test_strategy_inj', 1.0, '[]')
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
                (:o, 'test_strategy_inj', 'BTC/USDT', 'market', 1, 1.0,
                 'paper', 'filled', :decision_id, :t, :t, :v)
            """),
        {
            "o": VICTIM_ORDER,
            "decision_id": decision_id,
            "t": datetime(2024, 6, 1, tzinfo=UTC),
            "v": VICTIM_ACCOUNT,
        },
    )
    write_db.commit()
    return VICTIM_ORDER


@pytest.fixture
def registry(readonly_db) -> ChatToolRegistry:
    return ChatToolRegistry([GetTradeTool(ContextBuilder(readonly_db), readonly_db)])


def _dispatch(registry: ChatToolRegistry, session_account_id: str, block: ScriptedToolUseBlock):
    """Mirrors exactly what ChatQueryService (step 8) does with a
    parsed tool_use block: pass the LLM's own account_id along
    unmodified in llm_supplied_params (as if the LLM had been asked to
    supply one, or tried to on its own initiative) — the registry, not
    this test, is what must refuse it."""
    return registry.execute_tool_call(
        account_id=session_account_id,
        tool_name=block.name,
        llm_supplied_params=block.input,
    )


def test_llm_supplied_account_id_never_grants_access_to_another_account(registry, victim_trade):
    # The attacker's session is legitimately ATTACKER_ACCOUNT — but the
    # scripted LLM response tries to smuggle VICTIM_ACCOUNT into the
    # tool call's input, hoping the tool trusts it instead of the
    # session's real identity.
    scripted_response = ScriptedToolUseBlock(
        name="get_trade",
        input={"order_id": VICTIM_ORDER, "account_id": VICTIM_ACCOUNT},
    )

    result = _dispatch(registry, session_account_id=ATTACKER_ACCOUNT, block=scripted_response)

    # The injected account_id was discarded; the tool ran as
    # ATTACKER_ACCOUNT, which does not own VICTIM_ORDER, so it's
    # refused — never victim trade data.
    assert "error" in result
    assert "symbol" not in result
    assert "quantity" not in result


def test_legitimate_owner_can_still_retrieve_their_own_trade(registry, victim_trade):
    scripted_response = ScriptedToolUseBlock(name="get_trade", input={"order_id": VICTIM_ORDER})

    result = _dispatch(registry, session_account_id=VICTIM_ACCOUNT, block=scripted_response)

    assert result["order_id"] == VICTIM_ORDER
    assert result["symbol"] == "BTC/USDT"


def test_llm_requesting_a_nonexistent_write_style_tool_is_rejected(registry):
    for attempted_tool_name in [
        "cancel_order",
        "place_order",
        "update_risk_config",
        "delete_account",
    ]:
        scripted_response = ScriptedToolUseBlock(name=attempted_tool_name, input={})
        with pytest.raises(UnknownToolError):
            _dispatch(registry, session_account_id=ATTACKER_ACCOUNT, block=scripted_response)


def test_llm_requesting_a_plausible_but_unregistered_read_tool_is_also_rejected(registry):
    # Not every read-shaped-sounding name is real either — the registry
    # must never guess or fuzzy-match a tool name.
    scripted_response = ScriptedToolUseBlock(name="get_portfolio_summary", input={})
    with pytest.raises(UnknownToolError):
        _dispatch(registry, session_account_id=ATTACKER_ACCOUNT, block=scripted_response)
