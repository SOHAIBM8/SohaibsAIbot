"""
Wraps the Claude API. Checks LLMUsageTracker before every call and
refuses outright once the daily cap is reached — enforcement, not
decoration (decision #3). Own credentials via `api_key_env_var`,
entirely separate from any exchange API key storage/custody, which
doesn't exist yet (CLAUDE.md: Stage 2/3 of Live Execution are
unbuilt/unspecced).

Design note (rule 9): the real `anthropic` package is imported lazily,
only inside `_build_real_client()`, and is an optional dependency
(pyproject `[project.optional-dependencies].llm`) — every test in the
standard suite (test_llm_client.py) injects a fake `anthropic_client`
via the constructor, exactly like every other "no real network in unit
tests" component in this project (BinanceAdapter/FakeExchangeAdapter,
WebSocketConnection/fake_server, ...). The base install never needs the
anthropic package; only the small, separate, non-pytest real-API
integration test (spec section 5) does.

`tools` accepts a generic list of objects exposing `.name`/`.description`
(structurally — ChatTool, added in step 7, satisfies this without any
import here) rather than importing ChatTool directly, which would be a
step-4-depends-on-step-7 layering violation.

Design note (rule 9, added for step 8/ChatQueryService): the spec's
LLMResponse has only `tool_calls_made: list[str]` — tool NAMES, no
arguments. That's enough to log what happened, but not enough to
actually EXECUTE a requested tool call (GetTradeTool needs an
order_id, GetRegimeHistoryTool needs a symbol/window, ...) — a chat
feature that can name a tool but never pass it real arguments doesn't
work. `tool_call_requests` is an additive field alongside the spec'd
`tool_calls_made`, not a replacement for it, carrying each tool_use
block's id/name/input so ChatQueryService can actually dispatch them
through ChatToolRegistry.
"""

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.ai_assistant.llm_usage_tracker import LLMUsageTracker

# Flat per-1K-token estimate for a mid-tier Claude model. Deliberately
# crude (rule 8: no premature precision) — good enough for a running
# cost dashboard, not a billing-accurate figure. Revisit only if real
# usage shows this estimate is materially wrong.
_COST_PER_1K_TOKENS_USD = 0.006


class LLMUsageCapExceededError(RuntimeError):
    """Raised instead of proceeding when LLMUsageTracker refuses a
    call — the caller must handle this, never silently retry past it."""


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tokens_used: int
    tool_calls_made: list[str]
    model: str
    latency_ms: float
    tool_call_requests: list[ToolCallRequest] = field(default_factory=list)


class ToolLike(Protocol):
    name: str
    description: str


class AnthropicMessagesClient(Protocol):
    """Structural shape of the one Anthropic SDK call LLMClient needs —
    narrow on purpose so a fake test double never has to implement the
    whole SDK surface."""

    def create(self, **kwargs: Any) -> Any: ...


class LLMClient:
    def __init__(
        self,
        api_key_env_var: str,
        model: str,
        usage_tracker: LLMUsageTracker,
        anthropic_client: AnthropicMessagesClient | None = None,
    ):
        self.api_key_env_var = api_key_env_var
        self.model = model
        self.usage_tracker = usage_tracker
        # None in production until first real call, when a real client
        # is lazily built; always non-None in tests via injection.
        self._client = anthropic_client

    def generate(
        self,
        system_prompt: str,
        user_content: str,
        tools: Sequence[ToolLike] | None = None,
    ) -> LLMResponse:
        if not self.usage_tracker.check_and_increment():
            raise LLMUsageCapExceededError("daily LLM usage cap reached; call refused")

        client = self._client or self._build_real_client()
        tool_specs = (
            [{"name": t.name, "description": t.description} for t in tools] if tools else None
        )

        started = time.monotonic()
        raw = client.create(
            model=self.model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=tool_specs,
        )
        latency_ms = (time.monotonic() - started) * 1000

        response = self._parse(raw, self.model, latency_ms)
        self.usage_tracker.record_usage(
            tokens_used=response.tokens_used,
            cost_estimate=self.estimate_cost(response.tokens_used),
        )
        return response

    @staticmethod
    def estimate_cost(tokens_used: int) -> float:
        return (tokens_used / 1000) * _COST_PER_1K_TOKENS_USD

    def _build_real_client(self) -> AnthropicMessagesClient:
        import anthropic  # lazy: only needed for an actual network call

        api_key = os.environ[self.api_key_env_var]
        # anthropic has no type stubs (ignore_missing_imports handles
        # the import; .messages itself resolves to Any as a result).
        client: AnthropicMessagesClient = anthropic.Anthropic(api_key=api_key).messages
        return client

    @staticmethod
    def _parse(raw: Any, model: str, latency_ms: float) -> LLMResponse:
        text_parts = []
        tool_calls_made = []
        tool_call_requests = []
        for block in raw.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                tool_calls_made.append(block.name)
                tool_call_requests.append(
                    ToolCallRequest(
                        id=getattr(block, "id", block.name),
                        name=block.name,
                        input=getattr(block, "input", {}),
                    )
                )

        tokens_used = raw.usage.input_tokens + raw.usage.output_tokens
        return LLMResponse(
            text="".join(text_parts),
            tokens_used=tokens_used,
            tool_calls_made=tool_calls_made,
            model=getattr(raw, "model", model),
            latency_ms=latency_ms,
            tool_call_requests=tool_call_requests,
        )
