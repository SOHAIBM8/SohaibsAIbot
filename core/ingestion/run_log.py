"""
Shared writer for ingestion_run_log — every service (backfill,
incremental, gap_repair, data_quality) records exactly one row per
invocation through this one function, so the audit trail's shape can't
drift between services (spec section 6/7).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class RunLogEntry:
    run_type: str  # 'backfill' | 'incremental' | 'gap_repair' | 'data_quality'
    exchange: str
    symbol: str
    timeframe: str
    started_at: datetime
    finished_at: datetime
    status: str  # 'success' | 'partial' | 'failed'
    requested_range: dict
    received_count: int = 0
    stored_count: int = 0
    validation_failures: list[dict] = field(default_factory=list)
    retries: int = 0
    skipped_reason: str | None = None
    error_message: str | None = None


def record_run(db: Session, entry: RunLogEntry) -> int:
    result = db.execute(
        text("""
            INSERT INTO ingestion_run_log (
                run_type, exchange, symbol, timeframe, started_at, finished_at,
                status, requested_range, received_count, stored_count,
                validation_failures, retries, skipped_reason, error_message
            ) VALUES (
                :run_type, :exchange, :symbol, :timeframe, :started_at, :finished_at,
                :status, :requested_range, :received_count, :stored_count,
                :validation_failures, :retries, :skipped_reason, :error_message
            )
            RETURNING run_id
            """),
        {
            "run_type": entry.run_type,
            "exchange": entry.exchange,
            "symbol": entry.symbol,
            "timeframe": entry.timeframe,
            "started_at": entry.started_at,
            "finished_at": entry.finished_at,
            "status": entry.status,
            "requested_range": json.dumps(entry.requested_range, default=str),
            "received_count": entry.received_count,
            "stored_count": entry.stored_count,
            "validation_failures": json.dumps(entry.validation_failures, default=str),
            "retries": entry.retries,
            "skipped_reason": entry.skipped_reason,
            "error_message": entry.error_message,
        },
    )
    run_id: int = result.scalar_one()
    db.commit()
    return run_id
