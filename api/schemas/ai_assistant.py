"""
Pydantic schemas for the AI Assistant API. Visually distinguishing
"the deterministic system decided X" from "the AI is describing X"
(spec section 16) is a frontend rendering concern, but every schema
here carries enough structure (prompt_version, generated_at) for the
frontend to always label AI-generated content as such, never silently
alongside a raw deterministic fact.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChatRequestIn(BaseModel):
    question: str


class ChatResponseOut(BaseModel):
    answer: str


class ExplanationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    explanation_id: int
    subject_type: str
    subject_id: str
    generated_text: str
    prompt_version: str
    generated_at: datetime
