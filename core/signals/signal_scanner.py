"""
SignalScanner: the "give me trade signals" half of the previously
missing live loop (CLAUDE.md "no live execution loop exists"). This is
a deliberate, explicit SIGNAL-ONLY component — it never sizes a
position and never calls OrderManager.submit(); it reads real market
data, classifies regime, runs the real reference strategies, scores
confidence, records to signal_log, and publishes TradeSignalGenerated
for a human to act on. No kill switch / arming check exists here
because nothing here ever touches an exchange order.

Mirrors core/backtest_engine.py's per-bar loop shape (regime classify
-> per-strategy eligibility -> generate_signal), but over live data
read from raw_ohlcv instead of a pre-loaded DataFrame, and against
exactly ONE new bar per run rather than an entire historical series.

Data freshness is NOT this class's job. It assumes `raw_ohlcv` is kept
current by the existing ingestion Scheduler (BackfillService/
IncrementalUpdateService) for whatever (exchange, symbol, timeframe)
tracked_instruments row this scanner is pointed at — reusing that
already-built, already-tested pipeline rather than duplicating fetch
logic here. If there isn't enough history yet (still backfilling, or a
fresh install), run_once() logs and returns an empty result rather
than raising — this is a normal, expected transient state, not an
error.

RegimeDetector statefulness (core/regime_detector.py's own contract:
must be called once per bar, in chronological order) is handled by
`_seen_bar_times` — classify()/generate_signal() only run for a bar
this instance hasn't already processed, so a scanner polling more
often than a new bar appears is a safe no-op, not a double-count. This
in-memory set does NOT survive a process restart — regime hysteresis
starting fresh after a restart is an accepted, documented limitation,
same category as core/regime_detector.py's own "reset() at the start
of every new run" contract; persisting hysteresis state across
restarts was not part of what was asked for here and would be new
scope.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pandas as pd
import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.backtest_engine import REGIME_REQUIRED_FEATURES
from core.confidence_engine import ConfidenceEngine
from core.feature_store import FeatureRegistry, FeatureWindow
from core.ingestion.event_bus import EventBus
from core.regime_detector import RegimeDetector
from core.signals.events import TradeSignalGenerated
from core.strategy_base import StrategyBase

logger = structlog.get_logger(__name__)


@dataclass
class SignalScanResult:
    strategy_id: str
    symbol: str
    direction: int
    signal_strength: float
    confidence: float | None
    newly_recorded: bool


class SignalScanner:
    def __init__(
        self,
        db: Session,
        feature_registry: FeatureRegistry,
        strategies: list[StrategyBase],
        regime_detector: RegimeDetector,
        exchange: str,
        symbol: str,
        timeframe: str,
        confidence_engine: ConfidenceEngine | None = None,
        event_bus: EventBus | None = None,
        interval_seconds: float = 3600.0,
        lookback_bars: int = 250,
    ):
        self.db = db
        self.feature_registry = feature_registry
        self.strategies = strategies
        self.regime_detector = regime_detector
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.confidence_engine = confidence_engine
        self.event_bus = event_bus
        self.interval_seconds = interval_seconds
        self.lookback_bars = lookback_bars
        self._last_run_at: datetime | None = None
        self._last_processed_bar_time: datetime | None = None

        self._required_features = sorted(
            set(
                REGIME_REQUIRED_FEATURES
                + [
                    f
                    for s in strategies
                    for f in s.required_features
                    if f not in ("close", "open", "high", "low")
                ]
            )
        )

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self._last_run_at is None:
            return True
        return (now - self._last_run_at).total_seconds() >= self.interval_seconds

    def run_once(self, now: datetime | None = None) -> list[SignalScanResult]:
        now = now or datetime.now(UTC)
        self._last_run_at = now

        rows = self._fetch_recent_candles()
        if len(rows) < self.lookback_bars // 2:
            logger.info(
                "signal_scanner_insufficient_history",
                symbol=self.symbol,
                timeframe=self.timeframe,
                rows=len(rows),
            )
            return []

        df = pd.DataFrame(rows)
        df = self.feature_registry.compute(df, self._required_features)
        last = df.iloc[-1]
        if last[self._required_features].isna().any():
            logger.info(
                "signal_scanner_still_warming_up", symbol=self.symbol, timeframe=self.timeframe
            )
            return []

        bar_time = last["open_time"]
        if bar_time == self._last_processed_bar_time:
            # Polled again before a new bar closed — a safe no-op, not
            # a re-classification (see module docstring on why calling
            # RegimeDetector.classify() twice for the same bar would be
            # wrong, not just wasteful).
            return []
        self._last_processed_bar_time = bar_time

        window = FeatureWindow(
            symbol=self.symbol, timeframe=self.timeframe, as_of=bar_time, values=last.to_dict()
        )
        regime_state = self.regime_detector.classify(window)

        results: list[SignalScanResult] = []
        for strategy in self.strategies:
            eligible = regime_state.trend in strategy.meta.works_best_in and (
                not strategy.meta.works_best_in_vol
                or regime_state.vol in strategy.meta.works_best_in_vol
            )
            if not eligible:
                self._record_signal(
                    strategy_id=strategy.meta.strategy_id,
                    bar_time=bar_time,
                    regime_trend=regime_state.trend.value,
                    regime_vol=regime_state.vol.value,
                    regime_confidence=regime_state.trend_confidence,
                    direction=0,
                    signal_strength=0.0,
                    confidence=None,
                    reasons=[],
                    rejected_reasons=["not eligible for current regime"],
                )
                continue

            signal = strategy.generate_signal(window)
            confidence = None
            if signal.direction != 0 and self.confidence_engine is not None:
                confidence = self.confidence_engine.evaluate(signal, regime_state).confidence

            newly_recorded = self._record_signal(
                strategy_id=strategy.meta.strategy_id,
                bar_time=bar_time,
                regime_trend=regime_state.trend.value,
                regime_vol=regime_state.vol.value,
                regime_confidence=regime_state.trend_confidence,
                direction=signal.direction,
                signal_strength=signal.signal_strength,
                confidence=confidence,
                reasons=signal.reasons,
                rejected_reasons=signal.rejected_reasons,
            )

            if signal.direction != 0:
                results.append(
                    SignalScanResult(
                        strategy_id=strategy.meta.strategy_id,
                        symbol=self.symbol,
                        direction=signal.direction,
                        signal_strength=signal.signal_strength,
                        confidence=confidence,
                        newly_recorded=newly_recorded,
                    )
                )
                if newly_recorded and self.event_bus is not None:
                    self.event_bus.publish(
                        TradeSignalGenerated(
                            strategy_id=strategy.meta.strategy_id,
                            symbol=self.symbol,
                            direction=signal.direction,
                            signal_strength=signal.signal_strength,
                            confidence=confidence,
                            regime_trend=regime_state.trend.value,
                            regime_vol=regime_state.vol.value,
                            reasons=signal.reasons,
                            occurred_at=now,
                        )
                    )

        return results

    def _fetch_recent_candles(self) -> list[dict]:
        rows = (
            self.db.execute(
                text("""
                    SELECT open_time, open, high, low, close, volume
                    FROM raw_ohlcv
                    WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                    ORDER BY open_time DESC
                    LIMIT :limit
                    """),
                {
                    "exchange": self.exchange,
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "limit": self.lookback_bars,
                },
            )
            .mappings()
            .all()
        )
        # psycopg2 returns NUMERIC columns as Decimal — pandas-ta's
        # underlying numpy ufuncs (e.g. isnan) reject a Decimal-dtype
        # column outright, the same cast core/ingestion/data_quality_service.py's
        # _row_to_candle() already does for the same reason.
        return [
            {
                "open_time": r["open_time"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
            for r in reversed(rows)  # chronological order for feature computation
        ]

    def _record_signal(
        self,
        strategy_id: str,
        bar_time: datetime,
        regime_trend: str,
        regime_vol: str,
        regime_confidence: float,
        direction: int,
        signal_strength: float,
        confidence: float | None,
        reasons: list[str],
        rejected_reasons: list[str],
    ) -> bool:
        """Returns True only if this call actually inserted a new
        signal_log row — False for a bar this scanner (or a prior
        process) already logged for this strategy, matching
        GapDetectionService's/ExternalTradeDetectionService's "only
        report/publish on a genuinely new row" discipline."""
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                    INSERT INTO signal_log
                        (symbol, bar_time, strategy_id, regime, vol_regime, regime_confidence,
                         direction, signal_strength, confidence, reasons, rejected_reasons,
                         acted_on)
                    VALUES
                        (:symbol, :bar_time, :strategy_id, :regime, :vol_regime,
                         :regime_confidence, :direction, :signal_strength, :confidence,
                         :reasons, :rejected_reasons, FALSE)
                    ON CONFLICT (strategy_id, symbol, bar_time) DO NOTHING
                    """),
                {
                    "symbol": self.symbol,
                    "bar_time": bar_time,
                    "strategy_id": strategy_id,
                    "regime": regime_trend,
                    "vol_regime": regime_vol,
                    "regime_confidence": regime_confidence,
                    "direction": direction,
                    "signal_strength": signal_strength,
                    "confidence": confidence,
                    "reasons": reasons,
                    "rejected_reasons": rejected_reasons,
                },
            ),
        )
        self.db.commit()
        return result.rowcount > 0
