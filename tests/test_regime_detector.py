import pytest

from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.strategy_base import Regime, VolRegime


class FakeWindow:
    """Minimal stand-in for FeatureWindow — just needs .get()."""
    def __init__(self, **values):
        self._values = values

    def get(self, name):
        return self._values[name]


@pytest.fixture
def config():
    return RegimeDetectorConfig(
        trend_adx_threshold=20.0,
        vol_high_percentile=0.70,
        vol_low_percentile=0.30,
        min_confirmation_bars=3,
    )


@pytest.fixture
def detector(config):
    return RegimeDetector(config)


def bull_window(adx=30.0, atr_pct=0.5):
    return FakeWindow(ema_20=110, ema_50=100, adx_14=adx, atr_percentile_90=atr_pct)


def bear_window(adx=30.0, atr_pct=0.5):
    return FakeWindow(ema_20=90, ema_50=100, adx_14=adx, atr_percentile_90=atr_pct)


def sideways_window(adx=10.0, atr_pct=0.5):
    return FakeWindow(ema_20=110, ema_50=100, adx_14=adx, atr_percentile_90=atr_pct)


# --- trend classification -------------------------------------------------

def test_bull_trend_requires_ema_alignment_and_sufficient_adx(detector):
    # min_confirmation_bars=3 in the fixture config — feed 3 agreeing
    # bars so hysteresis actually confirms the new regime.
    for _ in range(3):
        state = detector.classify(bull_window(adx=30.0))
    assert state.trend == Regime.BULL_TREND


def test_bear_trend_requires_ema_alignment_and_sufficient_adx(detector):
    for _ in range(3):
        state = detector.classify(bear_window(adx=30.0))
    assert state.trend == Regime.BEAR_TREND


def test_low_adx_forces_sideways_even_with_ema_alignment(detector):
    """This is the whole point of the ADX filter: EMA order alone
    isn't enough to call something a trend."""
    state = detector.classify(sideways_window(adx=5.0))
    assert state.trend == Regime.SIDEWAYS


def test_trend_confidence_increases_with_adx(config):
    d1, d2 = RegimeDetector(config), RegimeDetector(config)
    weak = d1.classify(bull_window(adx=22.0))
    strong = d2.classify(bull_window(adx=45.0))
    assert strong.trend_confidence > weak.trend_confidence


# --- volatility classification --------------------------------------------

def test_high_atr_percentile_classified_high_vol(detector):
    for _ in range(3):
        state = detector.classify(bull_window(atr_pct=0.85))
    assert state.vol == VolRegime.HIGH_VOL


def test_low_atr_percentile_classified_low_vol(detector):
    for _ in range(3):
        state = detector.classify(bull_window(atr_pct=0.10))
    assert state.vol == VolRegime.LOW_VOL


def test_mid_atr_percentile_classified_normal_vol(detector):
    state = detector.classify(bull_window(atr_pct=0.50))
    assert state.vol == VolRegime.NORMAL_VOL


# --- hysteresis -------------------------------------------------------------

def test_single_bar_flip_does_not_change_confirmed_trend(detector):
    """Starts SIDEWAYS by default. One bull bar alone shouldn't be
    enough to confirm a new trend given min_confirmation_bars=3."""
    state = detector.classify(bull_window())
    assert state.trend == Regime.SIDEWAYS  # not yet confirmed


def test_trend_confirms_after_min_confirmation_bars(detector):
    for _ in range(2):
        state = detector.classify(bull_window())
        assert state.trend == Regime.SIDEWAYS  # still pending
    state = detector.classify(bull_window())
    assert state.trend == Regime.BULL_TREND  # 3rd consecutive bar confirms


def test_flapping_input_never_confirms_a_switch(detector):
    """Alternating bull/sideways bars should never accumulate 3
    consecutive agreeing bars — this is the exact scenario hysteresis
    exists to prevent."""
    for _ in range(10):
        state = detector.classify(bull_window())
        state = detector.classify(sideways_window())
    assert state.trend == Regime.SIDEWAYS  # never confirmed bull


def test_reset_clears_pending_and_confirmed_state(detector):
    for _ in range(3):
        detector.classify(bull_window())
    assert detector.classify(bull_window()).trend == Regime.BULL_TREND

    detector.reset()

    state = detector.classify(sideways_window(adx=5.0))
    assert state.trend == Regime.SIDEWAYS  # back to default, no leaked state


# --- config loading ----------------------------------------------------------

def test_config_loads_from_yaml(tmp_path):
    config_file = tmp_path / "regime.yaml"
    config_file.write_text(
        "trend:\n  adx_threshold: 25.0\n"
        "volatility:\n  lookback_bars: 60\n  high_percentile: 0.8\n  low_percentile: 0.2\n"
        "hysteresis:\n  min_confirmation_bars: 5\n"
    )
    config = RegimeDetectorConfig.from_yaml(str(config_file))
    assert config.trend_adx_threshold == 25.0
    assert config.vol_lookback_bars == 60
    assert config.min_confirmation_bars == 5


def test_config_uses_defaults_for_missing_keys(tmp_path):
    config_file = tmp_path / "partial.yaml"
    config_file.write_text("trend:\n  adx_threshold: 15.0\n")
    config = RegimeDetectorConfig.from_yaml(str(config_file))
    assert config.trend_adx_threshold == 15.0
    assert config.min_confirmation_bars == 3  # default, unset in file
