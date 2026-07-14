"""
Orchestrates every registered NewsSourceAdapter -> news_articles.
Idempotent by construction (ON CONFLICT DO NOTHING on url), matching
every ingestion service in this project's "idempotent and every run is
logged" ethos — though unlike core.ingestion, there is no dedicated
run-log table for this simpler component (rule 8: one adapter,
occasional polling, doesn't need ingestion_run_log's full machinery
yet; run() returning a per-source stored-count dict is enough for now
to observe what happened).

One source's fetch failure is logged and skipped, not allowed to abort
the other sources' fetches in the same sweep.
"""

from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.ai_assistant.events import NewsIngested
from core.ai_assistant.news_source_adapter import RawArticle
from core.ai_assistant.news_source_registry import NewsSourceRegistry
from core.ingestion.event_bus import EventBus

logger = structlog.get_logger(__name__)


class NewsIngestionService:
    def __init__(
        self, db: Session, registry: NewsSourceRegistry, event_bus: EventBus | None = None
    ):
        self.db = db
        self.registry = registry
        self.event_bus = event_bus

    def run(self, since: datetime) -> dict[str, int]:
        stored_counts: dict[str, int] = {}
        for adapter in self.registry.get_all():
            try:
                articles = adapter.fetch_recent(since)
            except Exception:
                logger.exception("news_fetch_failed", source=adapter.source_name)
                continue

            stored = self._store(articles)
            stored_counts[adapter.source_name] = stored
            if self.event_bus is not None and stored > 0:
                self.event_bus.publish(
                    NewsIngested(
                        source=adapter.source_name,
                        article_count=stored,
                        occurred_at=datetime.now(UTC),
                    )
                )
        return stored_counts

    def _store(self, articles: list[RawArticle]) -> int:
        stored = 0
        for article in articles:
            result = cast(
                CursorResult,
                self.db.execute(
                    text("""
                    INSERT INTO news_articles
                        (source, url, title, published_at, ingested_at, raw_content)
                    VALUES
                        (:source, :url, :title, :published_at, :ingested_at, :raw_content)
                    ON CONFLICT (url) DO NOTHING
                    """),
                    {
                        "source": article.source,
                        "url": article.url,
                        "title": article.title,
                        "published_at": article.published_at,
                        "ingested_at": datetime.now(UTC),
                        "raw_content": article.raw_content,
                    },
                ),
            )
            if result.rowcount:
                stored += 1
        self.db.commit()
        return stored
