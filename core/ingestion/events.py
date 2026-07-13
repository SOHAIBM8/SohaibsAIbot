"""
Event payloads published on the EventBus (spec 4.9). Plain dataclasses
with a to_dict() rather than anything ORM-aware — events cross a
serialization boundary (LISTEN/NOTIFY payloads are text), so they must
always be trivially JSON-encodable.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IngestionEvent:
    event_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CandlesIngested(IngestionEvent):
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    count: int = 0
    run_id: int | None = None
    event_type: str = "CandlesIngested"


@dataclass(frozen=True)
class GapDetected(IngestionEvent):
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    gap_start: str = ""
    gap_end: str = ""
    event_type: str = "GapDetected"


@dataclass(frozen=True)
class GapRepaired(IngestionEvent):
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    gap_start: str = ""
    gap_end: str = ""
    event_type: str = "GapRepaired"


@dataclass(frozen=True)
class BackfillCompleted(IngestionEvent):
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    stored_count: int = 0
    event_type: str = "BackfillCompleted"


@dataclass(frozen=True)
class DataQualityIssueFound(IngestionEvent):
    exchange: str = ""
    symbol: str = ""
    timeframe: str = ""
    severity: str = ""
    detail: str = ""
    event_type: str = "DataQualityIssueFound"
