"""
get_or_generate() hashes context_fn()'s output (the exact grounding
facts about to be shown to the LLM) and looks for a matching
llm_explanations row for (subject_type, subject_id, grounding_fact_hash).
Hash match -> return the cached row, zero LLM calls. Hash miss ->
generate, persist, publish ExplanationGenerated.

Design note: the context (e.g. TradeExplanationContext) is hashed via
a stable JSON serialization (dataclasses.asdict + sort_keys), not
repr()/id() — the same underlying facts always hash identically
regardless of Python object identity, and any real change in the facts
(an amended fill, a reclassified regime) reliably produces a cache
miss rather than a false hit.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ai_assistant.events import ExplanationGenerated
from core.ai_assistant.llm_client import LLMClient
from core.ai_assistant.prompt_template import PromptTemplate
from core.ingestion.event_bus import EventBus


def _require_dataclass_instance(context: object) -> Any:
    """dataclasses.is_dataclass() accepts both instances and classes,
    which asdict() does not — narrows to Any (not DataclassInstance,
    which isn't importable at runtime without typing_extensions) so
    mypy stops flagging the instance/class union asdict() itself
    rejects at runtime via TypeError."""
    if not is_dataclass(context) or isinstance(context, type):
        raise TypeError("ContextBuilder must return a dataclass instance")
    return context


@dataclass
class Explanation:
    explanation_id: int
    subject_type: str
    subject_id: str
    generated_text: str
    prompt_version: str
    generated_at: datetime


class ExplanationCache:
    def __init__(self, db_session: Session, event_bus: EventBus | None = None):
        self.db = db_session
        self.event_bus = event_bus

    def get_or_generate(
        self,
        subject_type: str,
        subject_id: str,
        context_fn: Callable[[], object],
        template: PromptTemplate,
        llm_client: LLMClient,
    ) -> Explanation:
        context = context_fn()
        grounding_fact_hash = self._hash(context)

        cached = self._lookup(subject_type, subject_id, grounding_fact_hash)
        if cached is not None:
            return cached

        user_content = self._render_context(context)
        response = llm_client.generate(
            system_prompt=template.template_text, user_content=user_content
        )
        return self._persist(subject_type, subject_id, grounding_fact_hash, template, response)

    @staticmethod
    def _hash(context: object) -> str:
        payload = json.dumps(
            asdict(_require_dataclass_instance(context)), sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _render_context(context: object) -> str:
        return json.dumps(asdict(_require_dataclass_instance(context)), sort_keys=True, default=str)

    def _lookup(
        self, subject_type: str, subject_id: str, grounding_fact_hash: str
    ) -> Explanation | None:
        row = (
            self.db.execute(
                text("""
                    SELECT e.explanation_id, e.subject_type, e.subject_id, e.generated_text,
                           e.generated_at, t.version AS prompt_version
                    FROM llm_explanations e
                    JOIN prompt_templates t ON t.template_id = e.prompt_template_id
                    WHERE e.subject_type = :subject_type
                      AND e.subject_id = :subject_id
                      AND e.grounding_fact_hash = :grounding_fact_hash
                    ORDER BY e.generated_at DESC
                    LIMIT 1
                    """),
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "grounding_fact_hash": grounding_fact_hash,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return Explanation(**row)

    def _persist(
        self,
        subject_type: str,
        subject_id: str,
        grounding_fact_hash: str,
        template: PromptTemplate,
        response: Any,
    ) -> Explanation:
        generated_at = datetime.now(UTC)
        cost_estimate = LLMClient.estimate_cost(response.tokens_used)
        result = self.db.execute(
            text("""
                INSERT INTO llm_explanations
                    (subject_type, subject_id, grounding_fact_hash, prompt_template_id,
                     model_used, generated_text, generated_at, tokens_used, cost_estimate)
                VALUES
                    (:subject_type, :subject_id, :grounding_fact_hash, :prompt_template_id,
                     :model_used, :generated_text, :generated_at, :tokens_used, :cost_estimate)
                RETURNING explanation_id
                """),
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "grounding_fact_hash": grounding_fact_hash,
                "prompt_template_id": template.template_id,
                "model_used": response.model,
                "generated_text": response.text,
                "generated_at": generated_at,
                "tokens_used": response.tokens_used,
                "cost_estimate": cost_estimate,
            },
        )
        explanation_id = result.scalar_one()
        self.db.commit()

        explanation = Explanation(
            explanation_id=explanation_id,
            subject_type=subject_type,
            subject_id=subject_id,
            generated_text=response.text,
            prompt_version=template.version,
            generated_at=generated_at,
        )
        if self.event_bus is not None:
            self.event_bus.publish(
                ExplanationGenerated(
                    subject_type=subject_type, subject_id=subject_id, occurred_at=generated_at
                )
            )
        return explanation
