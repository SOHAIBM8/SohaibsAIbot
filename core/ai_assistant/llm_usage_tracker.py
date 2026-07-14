"""
Enforces the daily LLM call cap in code, not just in a config value
nothing reads (decision #3). `check_and_increment()` is the single
gate every caller (LLMClient, in step 4) must pass through before
making a real API call — it is check-then-increment in one method
specifically so a caller can never observe "not yet capped" and then
increment after the fact, which would leave a window where two
concurrent callers both pass the check.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.event_bus import EventBus


@dataclass
class UsageSnapshot:
    usage_date: date
    calls_made: int
    tokens_used: int
    estimated_cost: float
    daily_cap_calls: int
    daily_cap_reached: bool


class LLMUsageTracker:
    """One row per usage_date in llm_usage_daily. A new date's row is
    created lazily, with calls_made=0, the first time it's touched that
    day — there is no separate "reset" job, the reset is just a new
    PRIMARY KEY value.

    Design note (rule 9): the spec's constructor is
    `(daily_cap_calls, db_session)`. An optional `event_bus` param is
    added so LLMUsageCapReached (core/ai_assistant/events.py) can
    actually be published somewhere — nothing else in the spec holds
    both a usage_date transition and an EventBus reference at once.
    Published exactly once per day, on the exact call that flips
    daily_cap_reached from false to true, not on every subsequent
    refused call."""

    def __init__(
        self, daily_cap_calls: int, db_session: Session, event_bus: EventBus | None = None
    ):
        self.daily_cap_calls = daily_cap_calls
        self.db = db_session
        self.event_bus = event_bus

    def check_and_increment(self) -> bool:
        """Returns False (and does NOT increment) if the cap is already
        reached — caller must refuse the request, not proceed anyway."""
        today = self._today()
        self._ensure_row(today)

        row = (
            self.db.execute(
                text("""
                    SELECT calls_made, daily_cap_reached
                    FROM llm_usage_daily
                    WHERE usage_date = :usage_date
                    FOR UPDATE
                    """),
                {"usage_date": today},
            )
            .mappings()
            .first()
        )
        assert row is not None  # _ensure_row just guaranteed this

        if row["daily_cap_reached"] or row["calls_made"] >= self.daily_cap_calls:
            self.db.execute(
                text("""
                    UPDATE llm_usage_daily SET daily_cap_reached = TRUE
                    WHERE usage_date = :usage_date AND daily_cap_reached = FALSE
                    """),
                {"usage_date": today},
            )
            self.db.commit()
            return False

        new_calls_made = row["calls_made"] + 1
        cap_reached_now = new_calls_made >= self.daily_cap_calls
        self.db.execute(
            text("""
                UPDATE llm_usage_daily
                SET calls_made = :calls_made, daily_cap_reached = :cap_reached
                WHERE usage_date = :usage_date
                """),
            {"calls_made": new_calls_made, "cap_reached": cap_reached_now, "usage_date": today},
        )
        self.db.commit()

        if cap_reached_now and self.event_bus is not None:
            from core.ai_assistant.events import LLMUsageCapReached

            self.event_bus.publish(LLMUsageCapReached(date=today, occurred_at=datetime.now(UTC)))
        return True

    def record_usage(self, tokens_used: int, cost_estimate: float) -> None:
        today = self._today()
        self._ensure_row(today)
        self.db.execute(
            text("""
                UPDATE llm_usage_daily
                SET tokens_used = tokens_used + :tokens_used,
                    estimated_cost = estimated_cost + :cost_estimate
                WHERE usage_date = :usage_date
                """),
            {
                "tokens_used": tokens_used,
                "cost_estimate": cost_estimate,
                "usage_date": today,
            },
        )
        self.db.commit()

    def snapshot(self, usage_date: date | None = None) -> UsageSnapshot:
        usage_date = usage_date or self._today()
        self._ensure_row(usage_date)
        row = (
            self.db.execute(
                text("""
                    SELECT usage_date, calls_made, tokens_used, estimated_cost,
                           daily_cap_calls, daily_cap_reached
                    FROM llm_usage_daily
                    WHERE usage_date = :usage_date
                    """),
                {"usage_date": usage_date},
            )
            .mappings()
            .first()
        )
        assert row is not None
        return UsageSnapshot(
            usage_date=row["usage_date"],
            calls_made=row["calls_made"],
            tokens_used=row["tokens_used"],
            estimated_cost=float(row["estimated_cost"]),
            daily_cap_calls=row["daily_cap_calls"],
            daily_cap_reached=row["daily_cap_reached"],
        )

    def _today(self) -> date:
        """Isolated as its own method so a test can pin "today" without
        monkeypatching the datetime module — subclass and override this
        one method instead of duplicating check_and_increment's logic."""
        return datetime.now(UTC).date()

    def _ensure_row(self, usage_date: date) -> None:
        self.db.execute(
            text("""
                INSERT INTO llm_usage_daily (usage_date, daily_cap_calls)
                VALUES (:usage_date, :daily_cap_calls)
                ON CONFLICT (usage_date) DO NOTHING
                """),
            {"usage_date": usage_date, "daily_cap_calls": self.daily_cap_calls},
        )
        self.db.commit()
