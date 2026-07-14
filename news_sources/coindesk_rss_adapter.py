"""
First concrete NewsSourceAdapter — CoinDesk's public RSS feed. No API
key required, keeps step 6 dependency-free (rule 8: build the simple
correct version first). Parsed with stdlib XML (no new dependency,
matching BinanceAdapter's requests-only footprint).

fetch_recent() raises on an actual network/parse failure rather than
swallowing it — NewsIngestionService (the only caller) is what decides
whether one source's failure should stop a sweep, exactly like
ExchangeAdapter callers own that decision for ingestion.
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from core.ai_assistant.news_source_adapter import NewsSourceAdapter, RawArticle

FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"


class CoinDeskRSSAdapter(NewsSourceAdapter):
    source_name = "coindesk"

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
        feed_url: str = FEED_URL,
    ):
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._feed_url = feed_url

    def fetch_recent(self, since: datetime) -> list[RawArticle]:
        response = self._session.get(self._feed_url, timeout=self._timeout)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        articles = []
        for item in root.findall("./channel/item"):
            published_at = _parse_pubdate(_text(item, "pubDate"))
            if published_at is not None and published_at < since:
                continue
            articles.append(
                RawArticle(
                    source=self.source_name,
                    url=_text(item, "link") or "",
                    title=_text(item, "title") or "",
                    published_at=published_at,
                    raw_content=_text(item, "description"),
                )
            )
        return articles


def _text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text is not None else None


def _parse_pubdate(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
