"""
Versioned prompt templates — the ONLY place system-prompt wording is
allowed to live for this component (docs/ai_assistant_spec.md decision
#5). Every generated explanation references a prompt_template_id, so
what was actually asked of the LLM is always reproducible after the
fact — the same reasoning ExperimentConfig.code_commit_hash applies to
backtests, applied here to prompts.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class PromptTemplate:
    template_id: str
    version: str
    subject_type: str  # 'trade' | 'risk_decision' | 'regime' | 'daily_summary' | 'chat'
    template_text: str
    created_at: datetime


class PromptTemplateRegistry:
    """Reads/writes the `prompt_templates` table. `get()` never falls
    back to a default or an in-code string when a template_id is
    missing — an unregistered/untraceable prompt would defeat decision
    #5's entire point, so a missing id is a hard error, not a silent
    default."""

    def __init__(self, db: Session):
        self.db = db

    def register(self, template: PromptTemplate) -> None:
        self.db.execute(
            text("""
                INSERT INTO prompt_templates
                    (template_id, version, subject_type, template_text, created_at)
                VALUES
                    (:template_id, :version, :subject_type, :template_text, :created_at)
                ON CONFLICT (template_id) DO UPDATE SET
                    version = EXCLUDED.version,
                    subject_type = EXCLUDED.subject_type,
                    template_text = EXCLUDED.template_text,
                    created_at = EXCLUDED.created_at
                """),
            {
                "template_id": template.template_id,
                "version": template.version,
                "subject_type": template.subject_type,
                "template_text": template.template_text,
                "created_at": template.created_at,
            },
        )
        self.db.commit()

    def get(self, template_id: str) -> PromptTemplate:
        row = (
            self.db.execute(
                text("""
                    SELECT template_id, version, subject_type, template_text, created_at
                    FROM prompt_templates
                    WHERE template_id = :template_id
                    """),
                {"template_id": template_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(f"no prompt template registered with template_id={template_id}")
        return PromptTemplate(**row)
