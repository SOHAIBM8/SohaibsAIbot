"""
Assembles the AI Assistant component's dependency graph for the API
layer — ChatQueryService/ExplanationCache/ContextBuilder are all
already-built, already-tested core classes; this module only wires
them together per-request, mirroring exactly the construction shape
tests/test_ai_assistant/test_chat_query_service_integration.py already
proves end-to-end. No prompt wording, tool logic, or LLM-call logic
lives here.
"""

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from api.auth.dependencies import get_settings
from api.config import DashboardSettings
from api.db import get_db
from api.readonly_db import get_readonly_db
from core.ai_assistant.chat_query_service import ChatQueryService
from core.ai_assistant.chat_tool import (
    GetRegimeHistoryTool,
    GetRiskDecisionsTool,
    GetTradeTool,
    SearchNewsTool,
)
from core.ai_assistant.chat_tool_registry import ChatToolRegistry
from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.llm_client import LLMClient
from core.ai_assistant.llm_usage_tracker import LLMUsageTracker


def get_context_builder(
    readonly_db: Session = Depends(get_readonly_db),
) -> Generator[ContextBuilder, None, None]:
    yield ContextBuilder(readonly_db)


def get_llm_client(
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
) -> LLMClient:
    # anthropic_client=None: safe to construct with no ANTHROPIC_API_KEY
    # set (core/ai_assistant/llm_client.py imports the real SDK and
    # reads the env var lazily, only inside a real generate() call).
    tracker = LLMUsageTracker(daily_cap_calls=settings.llm_daily_cap_calls, db_session=db)
    return LLMClient(
        api_key_env_var=settings.llm_api_key_env_var,
        model=settings.llm_model,
        usage_tracker=tracker,
    )


def get_chat_query_service(
    db: Session = Depends(get_db),
    readonly_db: Session = Depends(get_readonly_db),
    llm_client: LLMClient = Depends(get_llm_client),
) -> ChatQueryService:
    context_builder = ContextBuilder(readonly_db)
    registry = ChatToolRegistry(
        [
            GetTradeTool(context_builder, readonly_db),
            GetRiskDecisionsTool(context_builder),
            GetRegimeHistoryTool(context_builder),
            SearchNewsTool(readonly_db),
        ]
    )
    return ChatQueryService(llm_client, registry, db)


def get_explanation_cache(db: Session = Depends(get_db)) -> ExplanationCache:
    return ExplanationCache(db)
