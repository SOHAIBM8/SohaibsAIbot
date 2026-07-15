"""
The first real, concrete implementation of core/confidence_engine.py's
`PerformanceStore` Protocol — queries the `signal_log` table directly
rather than re-running any bucketing/grouping logic ConfidenceEngine
already owns. Wired in to fix CLAUDE.md's "confidence_engine has zero
real callers" gap.

Bucketing note: `signal_log` has no stored signal-strength-bucket
column — the CASE expression below reproduces
`ConfidenceEngine._bucket()`'s exact thresholds (`> 0.66` high,
`> 0.33` medium, else low) so there is exactly one place those
boundaries are defined in code and this store's SQL can drift out of
sync with it silently; kept here as a literal copy with an explicit
warning comment instead, since a SQL CASE expression can't import a
Python staticmethod.

Only rows with a non-null `outcome` are counted — a signal that hasn't
been closed out yet (or whatever eventually writes `outcome`, which
nothing in this codebase does yet — see CLAUDE.md's known gaps) has no
resolved pnl to learn from.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.strategy_base import Regime, VolRegime

# Must match core/confidence_engine.py's ConfidenceEngine._bucket()
# exactly — see module docstring.
_BUCKET_CASE_SQL = """
    CASE
        WHEN signal_strength > 0.66 THEN 'high'
        WHEN signal_strength > 0.33 THEN 'medium'
        ELSE 'low'
    END
"""


@dataclass
class SignalPerformanceHistory:
    sample_size: int
    win_rate: float


class SignalPerformanceStore:
    def __init__(self, db: Session):
        self.db = db

    def query(
        self,
        strategy_id: str,
        regime: Regime,
        vol_regime: VolRegime,
        signal_strength_bucket: str,
    ) -> SignalPerformanceHistory:
        row = (
            self.db.execute(
                text(f"""
                SELECT
                    count(*) AS n,
                    count(*) FILTER (WHERE (outcome ->> 'pnl')::numeric > 0) AS wins
                FROM signal_log
                WHERE strategy_id = :strategy_id
                  AND regime = :regime
                  AND vol_regime = :vol_regime
                  AND outcome IS NOT NULL
                  AND ({_BUCKET_CASE_SQL}) = :bucket
                """),
                {
                    "strategy_id": strategy_id,
                    "regime": regime.value,
                    "vol_regime": vol_regime.value,
                    "bucket": signal_strength_bucket,
                },
            )
            .mappings()
            .first()
        )

        assert row is not None  # count(*) always returns exactly one row
        n = row["n"]
        wins = row["wins"]
        return SignalPerformanceHistory(sample_size=n, win_rate=(wins / n) if n > 0 else 0.0)
