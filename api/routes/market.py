"""
Live Market API (spec section 9/26) — scoped down after research, not
by default. What spec section 9 asks for and what's actually real
today diverge in two ways, both documented here rather than faked:

1. "Candlestick chart per symbol/timeframe from the WebSocket market
   data bridge (reusing the execution engine's normalized feed)" —
   core/marketdata/live_market_data_source.py only streams from a
   Stage-1 FAKE/simulated feed (per its own docstring; no real
   exchange is wired into it), exposes no subscribe/callback hook
   (only a polling get_last_price()), and does zero OHLCV aggregation
   — it discards volume/timestamp and keeps only the latest raw price
   per symbol. Building a WebSocket ticker bridge against it would
   mean streaming fake data and presenting it as live, or building
   substantial new plumbing (a callback hook on LiveMarketDataSource,
   a new EventBus event type, gateway wiring) for a feed that still
   isn't real. Neither is "exposing what already exists." Deferred —
   flagged in CLAUDE.md's known-limitations section, not silently
   skipped.

2. "A regime badge overlay from the current RegimeState" — RegimeDetector
   is stateful, in-memory-only, and nothing runs it against live bars
   today; there is no `regime_state` table anywhere in schema.sql (confirmed
   by direct search). The existing codebase's own answer to "how do you
   get regime for a period" (core/ai_assistant/context_builder.py's
   build_regime_context) is retrospective-only — it reads regime/
   regime_confidence already stamped onto signal_log by a past backtest
   run. There is no "current" regime source to badge a live chart with.
   Same class of gap as positions/equity-curve. Deferred.

What IS real and built here: historical OHLCV candles from raw_ohlcv
(TimescaleDB hypertable), populated by the already-built, already-
tested ingestion pipeline against real exchange data — genuinely more
real than LiveMarketDataSource's fake feed would have been.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session
from api.auth.session_store import DashboardSession
from api.db import get_db
from api.schemas.market import CandleOut
from core.ingestion.ohlcv_reader import OHLCVReader

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/candles", response_model=list[CandleOut])
def list_candles(
    exchange: str = Query(...),
    symbol: str = Query(...),
    timeframe: str = Query(...),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> list[CandleOut]:
    reader = OHLCVReader(db)
    candles = reader.list_candles(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=limit,
    )
    return [CandleOut.model_validate(c) for c in candles]
