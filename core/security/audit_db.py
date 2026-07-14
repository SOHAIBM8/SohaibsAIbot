"""
Dedicated write connection for credential_audit_log, bound to the
INSERT-only `credential_audit_writer` Postgres role (decision #4) —
mirrors core/ai_assistant/readonly_db.py's pattern exactly, inverted:
that module proves a role CANNOT write; this one proves a role can
ONLY insert, never update or delete, enforced at the database grant
level so no amount of application-code discipline can accidentally
grant itself more. See schema.sql's `credential_audit_writer` role/
grants and test_audit_log_immutability.py for the test that proves it.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

CREDENTIAL_AUDIT_WRITER_DATABASE_URL = os.environ.get(
    "CREDENTIAL_AUDIT_WRITER_DATABASE_URL",
    "postgresql+psycopg2://credential_audit_writer:credential_audit_writer_dev_password"
    "@localhost:5432/trading_platform",
)

audit_writer_engine = create_engine(CREDENTIAL_AUDIT_WRITER_DATABASE_URL, pool_pre_ping=True)
AuditWriterSessionLocal = sessionmaker(bind=audit_writer_engine, autoflush=False, autocommit=False)
