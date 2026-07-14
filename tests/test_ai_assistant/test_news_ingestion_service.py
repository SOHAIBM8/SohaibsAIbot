"""
Tests run against real local Postgres. A fake NewsSourceAdapter stands
in — no real network call — so idempotency (ON CONFLICT DO NOTHING on
url) and per-source failure isolation are exercised deterministically.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.ai_assistant.events import NewsIngested
from core.ai_assistant.news_ingestion_service import NewsIngestionService
from core.ai_assistant.news_source_adapter import NewsSourceAdapter, RawArticle
from core.ai_assistant.news_source_registry import NewsSourceRegistry
from core.db import SessionLocal


class FakeNewsSourceAdapter(NewsSourceAdapter):
    def __init__(self, source_name: str, articles: list[RawArticle], raises: bool = False):
        self.source_name = source_name
        self._articles = articles
        self._raises = raises
        self.fetch_calls = 0

    def fetch_recent(self, since: datetime) -> list[RawArticle]:
        self.fetch_calls += 1
        if self._raises:
            raise ConnectionError("simulated network failure")
        return self._articles


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_type, handler):
        raise NotImplementedError


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM news_articles WHERE url LIKE 'https://test.example/%'"))
        session.commit()
        session.close()


def make_article(n: int, source: str = "test_source") -> RawArticle:
    return RawArticle(
        source=source,
        url=f"https://test.example/article-{n}",
        title=f"Article {n}",
        published_at=datetime(2024, 6, 1, tzinfo=UTC),
        raw_content="body text",
    )


def test_run_stores_new_articles_and_reports_count(db):
    registry = NewsSourceRegistry()
    adapter = FakeNewsSourceAdapter("test_source", [make_article(1), make_article(2)])
    registry.register(adapter)
    service = NewsIngestionService(db, registry)

    counts = service.run(since=datetime(2024, 1, 1, tzinfo=UTC))

    assert counts == {"test_source": 2}
    rows = db.execute(
        text("SELECT count(*) FROM news_articles WHERE url LIKE 'https://test.example/%'")
    ).scalar_one()
    assert rows == 2


def test_run_is_idempotent_on_repeated_urls(db):
    registry = NewsSourceRegistry()
    adapter = FakeNewsSourceAdapter("test_source", [make_article(3)])
    registry.register(adapter)
    service = NewsIngestionService(db, registry)

    first = service.run(since=datetime(2024, 1, 1, tzinfo=UTC))
    second = service.run(since=datetime(2024, 1, 1, tzinfo=UTC))

    assert first == {"test_source": 1}
    assert second == {"test_source": 0}  # same url, already stored


def test_one_sources_failure_does_not_block_another_source(db):
    registry = NewsSourceRegistry()
    broken = FakeNewsSourceAdapter("broken_source", [], raises=True)
    healthy = FakeNewsSourceAdapter("healthy_source", [make_article(4, source="healthy_source")])
    registry.register(broken)
    registry.register(healthy)
    service = NewsIngestionService(db, registry)

    counts = service.run(since=datetime(2024, 1, 1, tzinfo=UTC))

    assert "broken_source" not in counts
    assert counts["healthy_source"] == 1


def test_run_publishes_news_ingested_only_when_articles_were_stored(db):
    registry = NewsSourceRegistry()
    registry.register(FakeNewsSourceAdapter("test_source", [make_article(5)]))
    registry.register(FakeNewsSourceAdapter("empty_source", []))
    event_bus = FakeEventBus()
    service = NewsIngestionService(db, registry, event_bus=event_bus)

    service.run(since=datetime(2024, 1, 1, tzinfo=UTC))

    assert len(event_bus.published) == 1
    assert isinstance(event_bus.published[0], NewsIngested)
    assert event_bus.published[0].source == "test_source"
    assert event_bus.published[0].article_count == 1
