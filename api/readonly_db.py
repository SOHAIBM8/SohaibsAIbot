"""
FastAPI dependency yielding a request-scoped session bound to the
`llm_readonly` Postgres role (core.ai_assistant.readonly_db), for
every AI Assistant route that touches ContextBuilder/ChatTool — never
core.db.SessionLocal. Mirrors api/db.py's shape exactly; a separate
module because the underlying engine/session factory is genuinely
different (enforced at the Postgres grant level, per
core/ai_assistant/readonly_db.py's own docstring), not a stylistic
choice.
"""

from collections.abc import Generator

from sqlalchemy.orm import Session

from core.ai_assistant.readonly_db import ReadonlySessionLocal


def get_readonly_db() -> Generator[Session, None, None]:
    session = ReadonlySessionLocal()
    try:
        yield session
    finally:
        session.close()
