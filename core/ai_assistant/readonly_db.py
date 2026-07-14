"""
Dedicated read-only Postgres connection for ContextBuilder and every
ChatTool (docs/ai_assistant_spec.md decision #1). Deliberately a
second engine/session factory, not core.db.SessionLocal — the whole
point of this component's design is that its database access is
enforced by a Postgres role with no write grant of any kind, so no
amount of application-code discipline (or a bug, or a future
contributor) can accidentally give it one. See schema.sql's
`llm_readonly` role/grants for the enforced side of this guarantee,
and test_readonly_role_enforcement.py for the test that actually
proves it at the database layer.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

LLM_READONLY_DATABASE_URL = os.environ.get(
    "LLM_READONLY_DATABASE_URL",
    "postgresql+psycopg2://llm_readonly:llm_readonly_dev_password@localhost:5432/trading_platform",
)

readonly_engine = create_engine(LLM_READONLY_DATABASE_URL, pool_pre_ping=True)
ReadonlySessionLocal = sessionmaker(bind=readonly_engine, autoflush=False, autocommit=False)
