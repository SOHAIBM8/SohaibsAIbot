"""
Orchestrates LLMClient + ChatToolRegistry + llm_query_log. Every
question, every tool call resolved in answering it, and the final
response are logged unconditionally — this is the account-facing entry
point into the largest security surface in this component, so nothing
here is fire-and-forget.

Design note (rule 9): the spec's LLMClient.generate() signature is
(system_prompt, user_content, tools=None) — no conversation/message-
history parameter, so a full Anthropic-style multi-turn tool_result
round trip (assistant tool_use turn -> user tool_result turn) isn't
expressible through it as given. answer() instead does at most ONE
follow-up generate() call when the first response requests tool use:
it executes every requested tool via ChatToolRegistry (which enforces
account scoping, decision #2), then re-asks with the original question
plus the tool results folded into a single user_content string. This
is a deliberate simplification, not the full Anthropic tool-use
protocol — good enough to make chat actually useful and testable
against a fake LLM double without inventing a conversation-management
subsystem the spec never asked for. A second round of tool calls
requested by the follow-up response is not executed (no loop) — one
round trip is this step's scoped behavior.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ai_assistant.chat_tool_registry import ChatToolRegistry
from core.ai_assistant.llm_client import LLMClient

_SYSTEM_PROMPT = (
    "You are a read-only trading data assistant. Use the provided tools to look "
    "up the caller's own trading data. Never invent facts not returned by a tool."
)


class ChatQueryService:
    def __init__(self, llm_client: LLMClient, tool_registry: ChatToolRegistry, db_session: Session):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.db = db_session

    def answer(self, account_id: str, question: str) -> str:
        tools = self.tool_registry.get_all()
        response = self.llm_client.generate(
            system_prompt=_SYSTEM_PROMPT, user_content=question, tools=tools
        )

        tool_calls_made: list[str] = list(response.tool_calls_made)
        final_text = response.text

        if response.tool_call_requests:
            tool_results = [
                {
                    "tool": call.name,
                    "result": self._safe_execute(account_id, call.name, call.input),
                }
                for call in response.tool_call_requests
            ]
            follow_up_content = (
                f"{question}\n\nTool results:\n{json.dumps(tool_results, default=str)}"
            )
            follow_up = self.llm_client.generate(
                system_prompt=_SYSTEM_PROMPT, user_content=follow_up_content
            )
            tool_calls_made.extend(follow_up.tool_calls_made)
            final_text = follow_up.text

        self._log(account_id, question, tool_calls_made, final_text)
        return final_text

    def _safe_execute(self, account_id: str, tool_name: str, tool_input: dict) -> dict:
        try:
            return self.tool_registry.execute_tool_call(
                account_id=account_id, tool_name=tool_name, llm_supplied_params=dict(tool_input)
            )
        except Exception as exc:
            # An unknown/rejected tool call must still produce a
            # loggable, LLM-visible result rather than crashing the
            # whole query — the registry has already done the actual
            # security enforcement by the time this runs.
            return {"error": str(exc)}

    def _log(
        self, account_id: str, question: str, tool_calls_made: list[str], response_text: str
    ) -> None:
        self.db.execute(
            text("""
                INSERT INTO llm_query_log
                    (account_id, question, tool_calls_made, response, occurred_at)
                VALUES
                    (:account_id, :question, :tool_calls_made, :response, :occurred_at)
                """),
            {
                "account_id": account_id,
                "question": question,
                "tool_calls_made": json.dumps(tool_calls_made),
                "response": response_text,
                "occurred_at": datetime.now(UTC),
            },
        )
        self.db.commit()
