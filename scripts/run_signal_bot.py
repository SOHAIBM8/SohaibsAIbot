"""
The real, runnable entrypoint for the signal bot: discovers trade
signals continuously and notifies you (dashboard + email) — it never
places an order (see core/signals/signal_scanner.py's module docstring
for why: this is deliberately signal-only, not execution).

What it does, every hour (configurable via --interval-seconds):
  1. Keeps BTC/USDT 1h data fresh on Binance (real public market data,
     no API key needed) via the existing, already-tested ingestion
     Scheduler — backfills on first run, incremental updates after.
  2. Classifies the current market regime (trend + volatility).
  3. Runs BOTH reference strategies (EMA crossover, RSI mean
     reversion) against that regime.
  4. Scores each real signal's historical confidence via
     ConfidenceEngine (honest caveat: confidence will read
     "insufficient history" until enough real signal_log rows have
     accumulated from this bot actually running over time — that data
     doesn't exist on day one, and nothing fabricates it).
  5. Publishes a notification for every directional signal — visible
     in the dashboard's Notifications page immediately, and emailed if
     SMTP + notify_on_trade_signal are configured in Settings.

Run continuously:  python scripts/run_signal_bot.py
Run once and exit: python scripts/run_signal_bot.py --once
Needs: docker compose up -d (Postgres), schema.sql applied. No
Binance API credentials needed — this only ever reads public market
data, never places an order.
"""

import argparse
import sys
import time

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.confidence_engine import ConfidenceEngine
from core.db import SessionLocal
from core.indicators.register import build_default_registry
from core.ingestion.binance_adapter import BinanceAdapter
from core.ingestion.config import IngestionConfig
from core.ingestion.event_bus import PostgresEventBus
from core.ingestion.scheduler import Scheduler
from core.notifications.email_sender import EmailSender
from core.notifications.notification_log import NotificationLogStore
from core.notifications.notification_persister import NotificationPersister
from core.notifications.preferences_store import NotificationPreferencesStore
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.signal_performance_store import SignalPerformanceStore
from core.signals.signal_scanner import SignalScanner
from core.strategy_base import StrategyBase
from strategies.ema_cross import EMACrossStrategy
from strategies.rsi_mean_reversion import RSIMeanReversionStrategy

logger = structlog.get_logger(__name__)

EXCHANGE = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
ACCOUNT_ID = "default"  # matches DASHBOARD_ACCOUNT_ID's own default (api/config.py)


def _ensure_tracked(db: Session) -> None:
    db.execute(
        text("""
            INSERT INTO tracked_instruments (exchange, symbol, timeframe, active)
            VALUES (:exchange, :symbol, :timeframe, TRUE)
            ON CONFLICT (exchange, symbol, timeframe) DO UPDATE SET active = TRUE
            """),
        {"exchange": EXCHANGE, "symbol": SYMBOL, "timeframe": TIMEFRAME},
    )
    db.commit()


def build_scheduler(signal_interval_seconds: float) -> Scheduler:
    db = SessionLocal()
    _ensure_tracked(db)

    event_bus = PostgresEventBus()

    notification_persister = NotificationPersister(
        event_bus,
        store_factory=lambda: NotificationLogStore(SessionLocal()),
        preferences_store_factory=lambda: NotificationPreferencesStore(SessionLocal()),
        email_sender=EmailSender(),
        account_id=ACCOUNT_ID,
    )
    notification_persister.start()
    event_bus.start()

    strategies: list[StrategyBase] = [EMACrossStrategy(), RSIMeanReversionStrategy()]
    confidence_engine = ConfidenceEngine(performance_store=SignalPerformanceStore(db))

    signal_scanner = SignalScanner(
        db=db,
        feature_registry=build_default_registry(),
        strategies=strategies,
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        confidence_engine=confidence_engine,
        event_bus=event_bus,
        interval_seconds=signal_interval_seconds,
    )

    return Scheduler(
        db=db,
        adapters={EXCHANGE: BinanceAdapter()},
        config=IngestionConfig(),
        event_bus=event_bus,
        signal_scanner=signal_scanner,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Run a single sweep and exit, instead of looping."
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=3600.0,
        help="How often the signal scanner itself checks for a new signal (default: 3600 = 1h). "
        "The ingestion sweep that keeps market data fresh runs more often than this internally.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=30.0,
        help="How often the outer Scheduler loop wakes up to check whether anything is due "
        "(default: 30s) — only relevant without --once.",
    )
    args = parser.parse_args()

    print(
        f"Signal bot starting: {SYMBOL} {TIMEFRAME} on {EXCHANGE}, signal-only (no orders placed)."
    )
    print(
        f"Strategies: EMA crossover, RSI mean reversion. "
        f"Notifications -> account_id={ACCOUNT_ID!r}."
    )
    print("Configure email delivery in the dashboard's Settings page (notify_on_trade_signal).")

    scheduler = build_scheduler(signal_interval_seconds=args.interval_seconds)

    if args.once:
        summary = scheduler.run_once()
        print(f"Sweep complete. New trade signals this run: {summary.trade_signals_generated}")
        return

    print(
        f"Running continuously (Ctrl+C to stop), outer poll every {args.poll_interval_seconds}s..."
    )
    try:
        scheduler.run_forever(poll_interval_seconds=args.poll_interval_seconds, sleep=time.sleep)
    except KeyboardInterrupt:
        print("\nStopping.")
        scheduler.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
