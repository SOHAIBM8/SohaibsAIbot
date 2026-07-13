"""
Rule-based regime detector (V1).

Trend and volatility are classified as two INDEPENDENT dimensions
rather than one combined label — a bull trend can occur in either
high or low volatility, and collapsing both into a single enum would
force strategies to declare affinity for combinations they don't
actually have an opinion on.

No lookahead: every input feature used here (EMA20/50, ADX, ATR
percentile) is trailing-only by construction in the feature layer —
see core/indicators/derived.py for the ATR percentile's explicit
trailing-window guarantee.

Hysteresis: raw per-bar classification is checked against
min_confirmation_bars consecutive AGREEING bars before the *confirmed*
regime changes. Without this, indicators oscillating near their
threshold (ADX bouncing between 19 and 21, say) would flip strategy
eligibility on and off every few bars — the system would be trading
noise in the regime detector, not the market.
"""

from dataclasses import dataclass, field

from core.regime_config import RegimeDetectorConfig
from core.strategy_base import Regime, VolRegime


@dataclass
class RegimeState:
    trend: Regime
    trend_confidence: float
    vol: VolRegime
    vol_confidence: float
    reasons: list[str] = field(default_factory=list)


class RegimeDetector:
    """
    Stateful by design — unlike StrategyBase.generate_signal, which
    must be a pure function, hysteresis inherently requires remembering
    recent raw classifications across calls.

    IMPORTANT: classify() must be called once per bar, in strict
    chronological order. Call reset() at the start of every new
    backtest run/experiment, and whenever switching symbols — state
    from a previous run must never leak into the next one.
    """

    def __init__(self, config: RegimeDetectorConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._confirmed_trend: Regime = Regime.SIDEWAYS
        self._confirmed_vol: VolRegime = VolRegime.NORMAL_VOL
        self._pending_trend: Regime | None = None
        self._pending_trend_count: int = 0
        self._pending_vol: VolRegime | None = None
        self._pending_vol_count: int = 0

    def classify(self, feature_window: "FeatureWindow") -> RegimeState:
        raw_trend, trend_reason = self._classify_trend(feature_window)
        raw_vol, vol_reason = self._classify_vol(feature_window)

        confirmed_trend = self._apply_hysteresis_trend(raw_trend)
        confirmed_vol = self._apply_hysteresis_vol(raw_vol)

        return RegimeState(
            trend=confirmed_trend,
            trend_confidence=self._trend_confidence(feature_window),
            vol=confirmed_vol,
            vol_confidence=self._vol_confidence(feature_window),
            reasons=[trend_reason, vol_reason],
        )

    # --- trend axis ----------------------------------------------------

    def _classify_trend(self, fw) -> tuple[Regime, str]:
        """EMA20/EMA50 order gives direction; ADX gives strength. Order
        alone can't distinguish a real trend from EMAs drifting apart
        by noise in a choppy market — ADX filters that out."""
        ema_fast, ema_slow = fw.get("ema_20"), fw.get("ema_50")
        adx = fw.get("adx_14")

        if adx < self.config.trend_adx_threshold:
            return (
                Regime.SIDEWAYS,
                f"adx={adx:.1f} below trend threshold {self.config.trend_adx_threshold}",
            )
        if ema_fast > ema_slow:
            return Regime.BULL_TREND, f"ema20>ema50, adx={adx:.1f}"
        if ema_fast < ema_slow:
            return Regime.BEAR_TREND, f"ema20<ema50, adx={adx:.1f}"
        return Regime.SIDEWAYS, "ema20 == ema50"

    def _trend_confidence(self, fw) -> float:
        """Normalize ADX into 0-1: at the threshold, confidence is ~0;
        by ADX 50 (a commonly cited 'very strong trend' level),
        confidence saturates at 1.0."""
        adx = fw.get("adx_14")
        return max(0.0, min((adx - self.config.trend_adx_threshold) / 30.0, 1.0))

    def _apply_hysteresis_trend(self, raw: Regime) -> Regime:
        if raw == self._confirmed_trend:
            self._pending_trend, self._pending_trend_count = None, 0
            return self._confirmed_trend
        if raw == self._pending_trend:
            self._pending_trend_count += 1
        else:
            self._pending_trend, self._pending_trend_count = raw, 1
        if self._pending_trend_count >= self.config.min_confirmation_bars:
            self._confirmed_trend = raw
            self._pending_trend, self._pending_trend_count = None, 0
        return self._confirmed_trend

    # --- volatility axis -------------------------------------------------

    def _classify_vol(self, fw) -> tuple[VolRegime, str]:
        """Rank current ATR against its own trailing distribution
        (already computed as atr_percentile_90) rather than using ATR's
        raw price-denominated value — see derived.py for why."""
        pct = fw.get("atr_percentile_90")
        if pct >= self.config.vol_high_percentile:
            return VolRegime.HIGH_VOL, f"atr_pct={pct:.2f} >= {self.config.vol_high_percentile}"
        if pct <= self.config.vol_low_percentile:
            return VolRegime.LOW_VOL, f"atr_pct={pct:.2f} <= {self.config.vol_low_percentile}"
        return VolRegime.NORMAL_VOL, f"atr_pct={pct:.2f} within normal band"

    def _vol_confidence(self, fw) -> float:
        pct = fw.get("atr_percentile_90")
        dist = min(
            abs(pct - self.config.vol_high_percentile),
            abs(pct - self.config.vol_low_percentile),
        )
        return max(0.0, min(dist / 0.3, 1.0))

    def _apply_hysteresis_vol(self, raw: VolRegime) -> VolRegime:
        if raw == self._confirmed_vol:
            self._pending_vol, self._pending_vol_count = None, 0
            return self._confirmed_vol
        if raw == self._pending_vol:
            self._pending_vol_count += 1
        else:
            self._pending_vol, self._pending_vol_count = raw, 1
        if self._pending_vol_count >= self.config.min_confirmation_bars:
            self._confirmed_vol = raw
            self._pending_vol, self._pending_vol_count = None, 0
        return self._confirmed_vol
