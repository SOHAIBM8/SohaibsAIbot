"""
Pydantic schemas mirroring core.ingestion.ohlcv_reader.CandleRecord
field-for-field (spec section 4).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CandleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exchange: str
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
