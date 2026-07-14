"""
Event dataclasses published on the shared EventBus (core.ingestion.event_bus,
generalized to the structural EventLike Protocol for exactly this kind
of cross-component reuse). Each satisfies EventLike structurally via
event_type/to_dict — no inheritance needed.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime


@dataclass
class ExplanationGenerated:
    subject_type: str
    subject_id: str
    occurred_at: datetime

    @property
    def event_type(self) -> str:
        return "ExplanationGenerated"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsIngested:
    source: str
    article_count: int
    occurred_at: datetime

    @property
    def event_type(self) -> str:
        return "NewsIngested"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatQueryAnswered:
    account_id: str
    query_id: int
    occurred_at: datetime

    @property
    def event_type(self) -> str:
        return "ChatQueryAnswered"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LLMUsageCapReached:
    date: date
    occurred_at: datetime

    @property
    def event_type(self) -> str:
        return "LLMUsageCapReached"

    def to_dict(self) -> dict:
        return asdict(self)
