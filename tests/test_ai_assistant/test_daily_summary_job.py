"""
Tests run against real local Postgres, using a fake LLMClient (same
pattern as test_explanation_cache.py) so no network call is needed to
prove the wiring end to end: ContextBuilder -> ExplanationCache ->
LLMClient -> llm_explanations.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from core.ai_assistant.context_builder import ContextBuilder
from core.ai_assistant.daily_summary_job import DailySummaryJob
from core.ai_assistant.explanation_cache import ExplanationCache
from core.ai_assistant.prompt_template import PromptTemplate, PromptTemplateRegistry
from core.ai_assistant.readonly_db import ReadonlySessionLocal
from core.db import SessionLocal

ACCOUNT_ID = "test_daily_summary_account"


@dataclass
class FakeLLMResponse:
    text: str
    tokens_used: int
    tool_calls_made: list
    model: str
    latency_ms: float


class FakeLLMClient:
    def __init__(self):
        self.call_count = 0

    def generate(self, system_prompt, user_content, tools=None):
        self.call_count += 1
        return FakeLLMResponse(
            text="Account gained 5% today across 1 trade.",
            tokens_used=30,
            tool_calls_made=[],
            model="claude-fake-model",
            latency_ms=1.0,
        )


@pytest.fixture
def write_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM fills WHERE client_order_id LIKE 'test_co_dsj_%'"))
        session.execute(text("DELETE FROM orders WHERE client_order_id LIKE 'test_co_dsj_%'"))
        session.execute(
            text("DELETE FROM risk_decision_log WHERE strategy_id = 'test_strategy_dsj'")
        )
        session.execute(
            text("DELETE FROM account_snapshots WHERE account_id = :a"), {"a": ACCOUNT_ID}
        )
        session.execute(text("DELETE FROM paper_accounts WHERE account_id = :a"), {"a": ACCOUNT_ID})
        session.execute(
            text("DELETE FROM llm_explanations WHERE subject_id LIKE :p"), {"p": f"{ACCOUNT_ID}%"}
        )
        session.execute(
            text("DELETE FROM prompt_templates WHERE template_id = 'test_daily_summary_template'")
        )
        session.commit()
        session.close()


@pytest.fixture
def readonly_db():
    session = ReadonlySessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def template(write_db) -> PromptTemplate:
    t = PromptTemplate(
        template_id="test_daily_summary_template",
        version="1.0.0",
        subject_type="daily_summary",
        template_text="Summarize this account's trading day using only the given facts.",
        created_at=datetime.now(UTC),
    )
    PromptTemplateRegistry(write_db).register(t)
    return t


@pytest.fixture
def seeded_account(write_db):
    the_day = date(2024, 6, 1)
    day_start = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    day_mid = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    day_end = datetime(2024, 6, 1, 23, 59, tzinfo=UTC)

    write_db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 10000, 10500, :created_at)
            """),
        {"a": ACCOUNT_ID, "created_at": day_start},
    )
    write_db.execute(
        text("""
            INSERT INTO account_snapshots (account_id, equity, open_position_count, snapshot_at)
            VALUES (:a, 10000, 0, :day_start), (:a, 10500, 0, :day_end)
            """),
        {"a": ACCOUNT_ID, "day_start": day_start, "day_end": day_end},
    )

    decision_id = write_db.execute(
        text("""
            INSERT INTO risk_decision_log
                (bar_time, strategy_id, proposed_quantity, approved_quantity, layer_results)
            VALUES (:bar_time, 'test_strategy_dsj', 1.0, 1.0, '[]')
            RETURNING id
            """),
        {"bar_time": day_mid},
    ).scalar_one()

    write_db.execute(
        text("""
            INSERT INTO orders
                (client_order_id, strategy_id, symbol, order_type, direction, quantity,
                 mode, state, risk_decision_id, created_at, updated_at, account_id)
            VALUES
                ('test_co_dsj_1', 'test_strategy_dsj', 'BTC/USDT', 'market', 1, 1.0,
                 'paper', 'filled', :decision_id, :day_mid, :day_mid, :a)
            """),
        {"decision_id": decision_id, "day_mid": day_mid, "a": ACCOUNT_ID},
    )
    write_db.execute(
        text("""
            INSERT INTO fills (client_order_id, fill_price, quantity, fee, is_partial, filled_at)
            VALUES ('test_co_dsj_1', 65000.0, 1.0, 5.0, FALSE, :day_mid)
            """),
        {"day_mid": day_mid},
    )
    write_db.commit()
    return the_day


def test_run_for_account_generates_a_grounded_daily_summary(
    seeded_account, readonly_db, write_db, template
):
    context_builder = ContextBuilder(readonly_db)
    cache = ExplanationCache(write_db)
    llm_client = FakeLLMClient()
    job = DailySummaryJob(context_builder, cache, llm_client, template)

    explanation = job.run_for_account(ACCOUNT_ID, seeded_account)

    assert llm_client.call_count == 1
    assert explanation.subject_type == "daily_summary"
    assert explanation.subject_id == f"{ACCOUNT_ID}:2024-06-01"
    assert "5%" in explanation.generated_text


def test_run_for_account_is_cached_on_a_second_call(
    seeded_account, readonly_db, write_db, template
):
    context_builder = ContextBuilder(readonly_db)
    cache = ExplanationCache(write_db)
    llm_client = FakeLLMClient()
    job = DailySummaryJob(context_builder, cache, llm_client, template)

    first = job.run_for_account(ACCOUNT_ID, seeded_account)
    second = job.run_for_account(ACCOUNT_ID, seeded_account)

    assert llm_client.call_count == 1
    assert second.explanation_id == first.explanation_id


def test_scheduler_runs_daily_summary_for_a_due_account(
    seeded_account, readonly_db, write_db, template
):
    """Proves the actual spec requirement: DailySummaryJob is triggered
    BY the existing Scheduler (core.ingestion.scheduler), not invoked
    directly by test code — see Scheduler's daily_summary_job param and
    _run_daily_summaries()."""
    from core.ingestion.config import IngestionConfig
    from core.ingestion.scheduler import Scheduler

    context_builder = ContextBuilder(readonly_db)
    cache = ExplanationCache(write_db)
    llm_client = FakeLLMClient()
    job = DailySummaryJob(context_builder, cache, llm_client, template)

    scheduler = Scheduler(write_db, adapters={}, config=IngestionConfig(), daily_summary_job=job)
    summary = scheduler.run_once(now=datetime(2024, 6, 1, 12, 0, tzinfo=UTC))

    assert ACCOUNT_ID in summary.daily_summaries_run
    assert llm_client.call_count == 1

    row = (
        write_db.execute(
            text("SELECT last_daily_summary_at FROM paper_accounts WHERE account_id = :a"),
            {"a": ACCOUNT_ID},
        )
        .mappings()
        .first()
    )
    assert row["last_daily_summary_at"] is not None


def test_scheduler_skips_account_not_yet_due(seeded_account, readonly_db, write_db, template):
    from core.ingestion.config import IngestionConfig
    from core.ingestion.scheduler import Scheduler

    context_builder = ContextBuilder(readonly_db)
    cache = ExplanationCache(write_db)
    llm_client = FakeLLMClient()
    job = DailySummaryJob(context_builder, cache, llm_client, template)

    scheduler = Scheduler(write_db, adapters={}, config=IngestionConfig(), daily_summary_job=job)
    first_run_at = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    scheduler.run_once(now=first_run_at)
    assert llm_client.call_count == 1

    soon_after = first_run_at + timedelta(hours=1)
    summary = scheduler.run_once(now=soon_after)

    assert ACCOUNT_ID not in summary.daily_summaries_run
    assert llm_client.call_count == 1  # not called again


def test_run_for_account_without_snapshots_raises_lookup_error(readonly_db, write_db, template):
    write_db.execute(
        text("""
            INSERT INTO paper_accounts (account_id, starting_balance, current_cash, created_at)
            VALUES (:a, 10000, 10000, now())
            """),
        {"a": ACCOUNT_ID},
    )
    write_db.commit()

    context_builder = ContextBuilder(readonly_db)
    cache = ExplanationCache(write_db)
    llm_client = FakeLLMClient()
    job = DailySummaryJob(context_builder, cache, llm_client, template)

    with pytest.raises(LookupError, match="no account_snapshots row"):
        job.run_for_account(ACCOUNT_ID, date(2024, 6, 1))
