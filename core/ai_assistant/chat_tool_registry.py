"""
Structural copy of StrategyRegistry's discovery pattern (decision #4),
but its critical job is decision #2: `execute_tool_call()` is the one
choke point every tool invocation must pass through. It strips any
'account_id'/'user_id' key from LLM-supplied params BEFORE dispatch,
then calls tool.execute(account_id=<real session value>, **remaining).
No caller of this class — not even this class's own tests — can make a
tool see an account_id other than the one execute_tool_call() itself
was given.
"""

from core.ai_assistant.chat_tool import ChatTool

_INJECTABLE_IDENTITY_KEYS = {"account_id", "user_id"}


class UnknownToolError(ValueError):
    pass


class ChatToolRegistry:
    def __init__(self, tools: list[ChatTool]):
        self._tools = {t.name: t for t in tools}

    def execute_tool_call(self, account_id: str, tool_name: str, llm_supplied_params: dict) -> dict:
        if tool_name not in self._tools:
            raise UnknownToolError(f"unknown tool: {tool_name}")

        safe_params = {
            k: v for k, v in llm_supplied_params.items() if k not in _INJECTABLE_IDENTITY_KEYS
        }
        return self._tools[tool_name].execute(account_id=account_id, **safe_params)

    def get_all(self) -> list[ChatTool]:
        return list(self._tools.values())
