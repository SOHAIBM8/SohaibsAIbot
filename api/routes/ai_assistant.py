"""
AI Assistant API (spec section 16/26) — wraps ChatQueryService (chat)
and ExplanationCache (trade/risk-decision/daily-summary explanations).
No LLM prompt wording, tool logic, or grounding-fact assembly lives
here — see api/ai_assistant_deps.py and api/ai_assistant_templates.py
for where each of those genuinely lives (core/ai_assistant/ itself,
or a thin, explicitly-flagged registration step for this API's own
well-known template ids).

Explanation endpoints do their own account-ownership check before
calling ContextBuilder, mirroring GetTradeTool's discipline
(core/ai_assistant/chat_tool.py) — build_trade_context() itself takes
no account_id and does no ownership check, so the route must, or a
dashboard user could read another account's trade explanation by
order_id. risk_decision_log has no account_id column at all (same as
Step 4's /api/risk/decisions) — decisions aren't account-scoped in
this schema, single-operator V1, so no equivalent check applies there.
"""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.ai_assistant_deps import (
    get_chat_query_service,
    get_context_builder,
    get_explanation_cache,
    get_llm_client,
)
from api.ai_assistant_templates import (
    DAILY_SUMMARY_TEMPLATE_ID,
    RISK_DECISION_TEMPLATE_ID,
    TRADE_TEMPLATE_ID,
)
from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.ai_assistant import ChatRequestIn, ChatResponseOut, ExplanationOut
from core.ai_assistant.chat_query_service import ChatQueryService
from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.llm_client import LLMClient, LLMUsageCapExceededError
from core.ai_assistant.prompt_template import PromptTemplateRegistry
from core.execution.order_reader import OrderReader

router = APIRouter(prefix="/api/ai", tags=["ai_assistant"])


def _handle_llm_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, LLMUsageCapExceededError):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    missing_api_key = isinstance(exc, KeyError) and "ANTHROPIC_API_KEY" in str(exc)
    if missing_api_key or isinstance(exc, ImportError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI assistant is not configured on this server (no LLM API key/package).",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/chat", response_model=ChatResponseOut)
def chat(
    body: ChatRequestIn,
    service: ChatQueryService = Depends(get_chat_query_service),
    session: DashboardSession = Depends(get_current_session),
) -> ChatResponseOut:
    try:
        answer = service.answer(account_id=session.account_id, question=body.question)
    except (LLMUsageCapExceededError, KeyError, ImportError) as exc:
        raise _handle_llm_errors(exc) from exc
    return ChatResponseOut(answer=answer)


@router.get("/explanations/trade/{order_id}", response_model=ExplanationOut)
def explain_trade(
    order_id: str,
    db: Session = Depends(get_db),
    context_builder: ContextBuilder = Depends(get_context_builder),
    explanation_cache: ExplanationCache = Depends(get_explanation_cache),
    llm_client: LLMClient = Depends(get_llm_client),
    session: DashboardSession = Depends(get_current_session),
) -> ExplanationOut:
    order = OrderReader(db).get_order(order_id, account_id=session.account_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")

    template = PromptTemplateRegistry(db).get(TRADE_TEMPLATE_ID)
    try:
        explanation = explanation_cache.get_or_generate(
            subject_type="trade",
            subject_id=order_id,
            context_fn=lambda: context_builder.build_trade_context(order_id),
            template=template,
            llm_client=llm_client,
        )
    except (LLMUsageCapExceededError, KeyError, ImportError) as exc:
        raise _handle_llm_errors(exc) from exc
    return ExplanationOut.model_validate(explanation)


@router.get("/explanations/risk-decision/{decision_id}", response_model=ExplanationOut)
def explain_risk_decision(
    decision_id: int,
    db: Session = Depends(get_db),
    context_builder: ContextBuilder = Depends(get_context_builder),
    explanation_cache: ExplanationCache = Depends(get_explanation_cache),
    llm_client: LLMClient = Depends(get_llm_client),
    _session: DashboardSession = Depends(get_current_session),
) -> ExplanationOut:
    template = PromptTemplateRegistry(db).get(RISK_DECISION_TEMPLATE_ID)
    try:
        explanation = explanation_cache.get_or_generate(
            subject_type="risk_decision",
            subject_id=str(decision_id),
            context_fn=lambda: context_builder.build_risk_decision_context(decision_id),
            template=template,
            llm_client=llm_client,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="risk decision not found"
        ) from exc
    except (LLMUsageCapExceededError, ImportError) as exc:
        raise _handle_llm_errors(exc) from exc
    return ExplanationOut.model_validate(explanation)


@router.get("/daily-summary/{summary_date}", response_model=ExplanationOut)
def explain_daily_summary(
    summary_date: date_type,
    db: Session = Depends(get_db),
    context_builder: ContextBuilder = Depends(get_context_builder),
    explanation_cache: ExplanationCache = Depends(get_explanation_cache),
    llm_client: LLMClient = Depends(get_llm_client),
    session: DashboardSession = Depends(get_current_session),
) -> ExplanationOut:
    template = PromptTemplateRegistry(db).get(DAILY_SUMMARY_TEMPLATE_ID)
    subject_id = f"{session.account_id}:{summary_date.isoformat()}"
    try:
        explanation = explanation_cache.get_or_generate(
            subject_type="daily_summary",
            subject_id=subject_id,
            context_fn=lambda: context_builder.build_daily_summary_context(
                session.account_id, summary_date
            ),
            template=template,
            llm_client=llm_client,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (LLMUsageCapExceededError, ImportError) as exc:
        raise _handle_llm_errors(exc) from exc
    return ExplanationOut.model_validate(explanation)
