"""
Triggered by the existing Scheduler (core.ingestion.scheduler), not a
new scheduling mechanism (decision #7: explanation generation is
on-demand or nightly-scheduled only, never triggered synchronously
from a trading event). run_for_account() is the unit of work Scheduler
calls once per due account per sweep.
"""

from datetime import date

from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.explanation_cache import Explanation, ExplanationCache
from core.ai_assistant.llm_client import LLMClient
from core.ai_assistant.prompt_template import PromptTemplate


class DailySummaryJob:
    def __init__(
        self,
        context_builder: ContextBuilder,
        explanation_cache: ExplanationCache,
        llm_client: LLMClient,
        template: PromptTemplate,
    ):
        self.context_builder = context_builder
        self.explanation_cache = explanation_cache
        self.llm_client = llm_client
        self.template = template

    def run_for_account(self, account_id: str, for_date: date) -> Explanation:
        subject_id = f"{account_id}:{for_date.isoformat()}"
        return self.explanation_cache.get_or_generate(
            subject_type="daily_summary",
            subject_id=subject_id,
            context_fn=lambda: self.context_builder.build_daily_summary_context(
                account_id, for_date
            ),
            template=self.template,
            llm_client=self.llm_client,
        )
