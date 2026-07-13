"""
Risk parameters, loaded from YAML — same pattern as
core/regime_config.py. Kept separate from RiskEngine logic so tuning a
loss limit or a drawdown tier never requires touching orchestration
code, and so risk parameters are versioned/comparable across
experiments exactly like strategy versions already are (see
ExperimentConfig.risk_config_id).
"""

from dataclasses import dataclass

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class RiskConfig:
    risk_config_id: str = "default"
    version: str = "1.0.0"
    daily_loss_limit_pct: float = 0.03
    weekly_loss_limit_pct: float = 0.08
    drawdown_tier_1_pct: float = 0.10  # throttle threshold
    drawdown_tier_1_factor: float = 0.5  # e.g. 0.5 -> half size
    drawdown_tier_2_pct: float = 0.15  # hard stop on new entries
    drawdown_tier_3_pct: float = 0.25  # auto kill switch threshold
    max_gross_exposure_pct: float = 1.0
    max_net_exposure_pct: float = 0.5
    max_concurrent_positions: int = 5
    max_same_symbol_directional_exposure_pct: float = 0.2
    sizing_method: str = (
        "fixed_fraction"  # 'fixed_fraction' | 'volatility_adjusted' | 'fractional_kelly'
    )
    kelly_fraction_multiplier: float = 0.5
    kelly_min_sample_size: int = 30
    circuit_breaker_atr_percentile_threshold: float = 0.95
    circuit_breaker_confirmation_bars: int = 3
    kill_switch_auto_flatten: bool = False

    @classmethod
    def from_yaml(cls, path: str) -> "RiskConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        defaults = cls()
        loss_limits = raw.get("loss_limits", {})
        drawdown = raw.get("drawdown", {})
        exposure = raw.get("exposure", {})
        sizing = raw.get("sizing", {})
        circuit_breaker = raw.get("circuit_breaker", {})
        kill_switch = raw.get("kill_switch", {})
        return cls(
            risk_config_id=raw.get("risk_config_id", defaults.risk_config_id),
            version=raw.get("version", defaults.version),
            daily_loss_limit_pct=loss_limits.get(
                "daily_loss_limit_pct", defaults.daily_loss_limit_pct
            ),
            weekly_loss_limit_pct=loss_limits.get(
                "weekly_loss_limit_pct", defaults.weekly_loss_limit_pct
            ),
            drawdown_tier_1_pct=drawdown.get("tier_1_pct", defaults.drawdown_tier_1_pct),
            drawdown_tier_1_factor=drawdown.get("tier_1_factor", defaults.drawdown_tier_1_factor),
            drawdown_tier_2_pct=drawdown.get("tier_2_pct", defaults.drawdown_tier_2_pct),
            drawdown_tier_3_pct=drawdown.get("tier_3_pct", defaults.drawdown_tier_3_pct),
            max_gross_exposure_pct=exposure.get(
                "max_gross_exposure_pct", defaults.max_gross_exposure_pct
            ),
            max_net_exposure_pct=exposure.get(
                "max_net_exposure_pct", defaults.max_net_exposure_pct
            ),
            max_concurrent_positions=exposure.get(
                "max_concurrent_positions", defaults.max_concurrent_positions
            ),
            max_same_symbol_directional_exposure_pct=exposure.get(
                "max_same_symbol_directional_exposure_pct",
                defaults.max_same_symbol_directional_exposure_pct,
            ),
            sizing_method=sizing.get("method", defaults.sizing_method),
            kelly_fraction_multiplier=sizing.get(
                "kelly_fraction_multiplier", defaults.kelly_fraction_multiplier
            ),
            kelly_min_sample_size=sizing.get(
                "kelly_min_sample_size", defaults.kelly_min_sample_size
            ),
            circuit_breaker_atr_percentile_threshold=circuit_breaker.get(
                "atr_percentile_threshold", defaults.circuit_breaker_atr_percentile_threshold
            ),
            circuit_breaker_confirmation_bars=circuit_breaker.get(
                "confirmation_bars", defaults.circuit_breaker_confirmation_bars
            ),
            kill_switch_auto_flatten=kill_switch.get(
                "auto_flatten", defaults.kill_switch_auto_flatten
            ),
        )


def upsert_risk_config(db: Session, config: RiskConfig) -> None:
    """Persist a RiskConfig into the risk_config table. Nothing in the
    spec explicitly inserts a risk_config row anywhere, but
    risk_decision_log.risk_config_id is a FK into it — RiskEngine calls
    this on construction so that FK is always satisfiable (see
    risk_engine.py's design note #5)."""
    max_same_symbol_pct = config.max_same_symbol_directional_exposure_pct
    atr_pct_threshold = config.circuit_breaker_atr_percentile_threshold
    db.execute(
        text("""
            INSERT INTO risk_config (
                risk_config_id, version, daily_loss_limit_pct, weekly_loss_limit_pct,
                drawdown_tier_1_pct, drawdown_tier_1_factor, drawdown_tier_2_pct,
                drawdown_tier_3_pct, max_gross_exposure_pct, max_net_exposure_pct,
                max_concurrent_positions, max_same_symbol_directional_exposure_pct,
                sizing_method, kelly_fraction_multiplier, kelly_min_sample_size,
                circuit_breaker_atr_percentile_threshold, circuit_breaker_confirmation_bars,
                kill_switch_auto_flatten
            ) VALUES (
                :risk_config_id, :version, :daily_loss_limit_pct, :weekly_loss_limit_pct,
                :drawdown_tier_1_pct, :drawdown_tier_1_factor, :drawdown_tier_2_pct,
                :drawdown_tier_3_pct, :max_gross_exposure_pct, :max_net_exposure_pct,
                :max_concurrent_positions, :max_same_symbol_directional_exposure_pct,
                :sizing_method, :kelly_fraction_multiplier, :kelly_min_sample_size,
                :circuit_breaker_atr_percentile_threshold, :circuit_breaker_confirmation_bars,
                :kill_switch_auto_flatten
            )
            ON CONFLICT (risk_config_id) DO UPDATE SET
                version = EXCLUDED.version,
                daily_loss_limit_pct = EXCLUDED.daily_loss_limit_pct,
                weekly_loss_limit_pct = EXCLUDED.weekly_loss_limit_pct,
                drawdown_tier_1_pct = EXCLUDED.drawdown_tier_1_pct,
                drawdown_tier_1_factor = EXCLUDED.drawdown_tier_1_factor,
                drawdown_tier_2_pct = EXCLUDED.drawdown_tier_2_pct,
                drawdown_tier_3_pct = EXCLUDED.drawdown_tier_3_pct,
                max_gross_exposure_pct = EXCLUDED.max_gross_exposure_pct,
                max_net_exposure_pct = EXCLUDED.max_net_exposure_pct,
                max_concurrent_positions = EXCLUDED.max_concurrent_positions,
                max_same_symbol_directional_exposure_pct =
                    EXCLUDED.max_same_symbol_directional_exposure_pct,
                sizing_method = EXCLUDED.sizing_method,
                kelly_fraction_multiplier = EXCLUDED.kelly_fraction_multiplier,
                kelly_min_sample_size = EXCLUDED.kelly_min_sample_size,
                circuit_breaker_atr_percentile_threshold =
                    EXCLUDED.circuit_breaker_atr_percentile_threshold,
                circuit_breaker_confirmation_bars = EXCLUDED.circuit_breaker_confirmation_bars,
                kill_switch_auto_flatten = EXCLUDED.kill_switch_auto_flatten
            """),
        {
            "risk_config_id": config.risk_config_id,
            "version": config.version,
            "daily_loss_limit_pct": config.daily_loss_limit_pct,
            "weekly_loss_limit_pct": config.weekly_loss_limit_pct,
            "drawdown_tier_1_pct": config.drawdown_tier_1_pct,
            "drawdown_tier_1_factor": config.drawdown_tier_1_factor,
            "drawdown_tier_2_pct": config.drawdown_tier_2_pct,
            "drawdown_tier_3_pct": config.drawdown_tier_3_pct,
            "max_gross_exposure_pct": config.max_gross_exposure_pct,
            "max_net_exposure_pct": config.max_net_exposure_pct,
            "max_concurrent_positions": config.max_concurrent_positions,
            "max_same_symbol_directional_exposure_pct": max_same_symbol_pct,
            "sizing_method": config.sizing_method,
            "kelly_fraction_multiplier": config.kelly_fraction_multiplier,
            "kelly_min_sample_size": config.kelly_min_sample_size,
            "circuit_breaker_atr_percentile_threshold": atr_pct_threshold,
            "circuit_breaker_confirmation_bars": config.circuit_breaker_confirmation_bars,
            "kill_switch_auto_flatten": config.kill_switch_auto_flatten,
        },
    )
    db.commit()
