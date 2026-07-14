"""
No real network calls — a fake requests.Session stands in for
CoinDeskRSSAdapter's parsing test, and discover() is exercised against
the real news_sources/ package, proving actual plugin discovery works.
"""

from datetime import UTC, datetime

from core.ai_assistant.news_source_registry import NewsSourceRegistry
from news_sources.coindesk_rss_adapter import CoinDeskRSSAdapter

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>CoinDesk</title>
  <item>
    <title>Bitcoin rallies past resistance</title>
    <link>https://www.coindesk.com/markets/2024/06/01/btc-rally</link>
    <pubDate>Sat, 01 Jun 2024 12:00:00 GMT</pubDate>
    <description>Bitcoin broke through a key resistance level today.</description>
  </item>
  <item>
    <title>Old news, before the window</title>
    <link>https://www.coindesk.com/markets/2024/01/01/old-news</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
    <description>This should be filtered out by since=.</description>
  </item>
</channel>
</rss>
"""


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, content: bytes):
        self._content = content
        self.last_url = None

    def get(self, url, timeout=None):
        self.last_url = url
        return _FakeResponse(self._content)


def test_coindesk_adapter_parses_feed_and_filters_by_since():
    session = _FakeSession(SAMPLE_RSS.encode())
    adapter = CoinDeskRSSAdapter(session=session)

    articles = adapter.fetch_recent(since=datetime(2024, 5, 1, tzinfo=UTC))

    assert len(articles) == 1
    assert articles[0].title == "Bitcoin rallies past resistance"
    assert articles[0].url == "https://www.coindesk.com/markets/2024/06/01/btc-rally"
    assert articles[0].source == "coindesk"
    assert articles[0].published_at == datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def test_coindesk_adapter_returns_empty_list_when_everything_is_older_than_since():
    session = _FakeSession(SAMPLE_RSS.encode())
    adapter = CoinDeskRSSAdapter(session=session)

    articles = adapter.fetch_recent(since=datetime(2025, 1, 1, tzinfo=UTC))
    assert articles == []


def test_registry_register_and_get_all():
    registry = NewsSourceRegistry()
    adapter = CoinDeskRSSAdapter()
    registry.register(adapter)

    assert registry.get_all() == [adapter]


def test_registry_discover_finds_the_real_coindesk_adapter():
    registry = NewsSourceRegistry()
    registry.discover(package="news_sources")

    names = {a.source_name for a in registry.get_all()}
    assert "coindesk" in names
    assert registry.rejected() == {}
