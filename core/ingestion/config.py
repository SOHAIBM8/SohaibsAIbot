"""
Ingestion configuration, loaded from YAML — same pattern as
core/regime_config.py. Kept separate from service logic so tuning a
retry interval or a polling cadence never requires touching code.
"""

from dataclasses import dataclass, field

import yaml


@dataclass
class IngestionConfig:
    default_backfill_years: int = 5
    gap_repair_max_attempts: int = 3
    gap_repair_retry_interval_hours: int = 24
    # timeframe -> polling cadence in seconds, for IncrementalUpdateService/Scheduler
    incremental_polling_seconds: dict[str, int] = field(
        default_factory=lambda: {
            "1m": 60,
            "5m": 5 * 60,
            "15m": 15 * 60,
            "1h": 60 * 60,
            "4h": 4 * 60 * 60,
            "1d": 24 * 60 * 60,
        }
    )
    per_request_candle_limit: int = 1000
    volume_anomaly_zscore_threshold: float = 4.0
    data_quality_issue_severity_threshold: str = "warning"

    @classmethod
    def from_yaml(cls, path: str) -> "IngestionConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        defaults = cls()
        return cls(
            default_backfill_years=raw.get("backfill", {}).get(
                "default_backfill_years", defaults.default_backfill_years
            ),
            gap_repair_max_attempts=raw.get("gap_repair", {}).get(
                "max_attempts", defaults.gap_repair_max_attempts
            ),
            gap_repair_retry_interval_hours=raw.get("gap_repair", {}).get(
                "retry_interval_hours", defaults.gap_repair_retry_interval_hours
            ),
            incremental_polling_seconds=raw.get("incremental", {}).get(
                "polling_seconds", defaults.incremental_polling_seconds
            ),
            per_request_candle_limit=raw.get("backfill", {}).get(
                "per_request_candle_limit", defaults.per_request_candle_limit
            ),
            volume_anomaly_zscore_threshold=raw.get("data_quality", {}).get(
                "volume_anomaly_zscore_threshold", defaults.volume_anomaly_zscore_threshold
            ),
            data_quality_issue_severity_threshold=raw.get("data_quality", {}).get(
                "issue_severity_threshold", defaults.data_quality_issue_severity_threshold
            ),
        )
