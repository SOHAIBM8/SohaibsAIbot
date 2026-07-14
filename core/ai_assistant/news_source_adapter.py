"""
NewsSourceAdapter is the seam between "the news ingestion pipeline" and
a specific news source's API/feed — structural copy of ExchangeAdapter
(core.ingestion.exchange_adapter), same reasoning: a second adapter
(e.g. a CryptoPanic/NewsAPI implementation) implements this same
interface later, and NewsIngestionService never needs to change when a
new source is added.

RawArticle is colocated here rather than a separate types module,
mirroring core.ingestion.types.RawCandle's placement next to
ExchangeAdapter — this interface is its only real consumer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawArticle:
    source: str
    url: str
    title: str
    published_at: datetime | None
    raw_content: str | None


class NewsSourceAdapter(ABC):
    source_name: str

    @abstractmethod
    def fetch_recent(self, since: datetime) -> list[RawArticle]:
        """Fetch articles published at or after `since`. Returns an
        empty list if there's nothing new — never raises for "no new
        articles", only for an actual fetch/parse failure."""
