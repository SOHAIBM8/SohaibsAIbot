from core.risk.risk_config import RiskConfig


def test_defaults_are_sensible():
    config = RiskConfig()
    assert config.risk_config_id == "default"
    assert config.daily_loss_limit_pct == 0.03
    assert config.drawdown_tier_1_pct < config.drawdown_tier_2_pct < config.drawdown_tier_3_pct
    assert config.sizing_method == "fixed_fraction"
    assert config.kill_switch_auto_flatten is False


def test_loads_repo_default_config_file():
    config = RiskConfig.from_yaml("config/risk_engine.yaml")
    assert config.risk_config_id == "default"
    assert config.version == "1.0.0"
    assert config.daily_loss_limit_pct == 0.03
    assert config.weekly_loss_limit_pct == 0.08
    assert config.drawdown_tier_1_factor == 0.5
    assert config.max_concurrent_positions == 5
    assert config.kelly_min_sample_size == 30
    assert config.circuit_breaker_confirmation_bars == 3


def test_config_loads_from_yaml(tmp_path):
    config_file = tmp_path / "risk.yaml"
    config_file.write_text(
        "risk_config_id: aggressive\n"
        "version: '2.0.0'\n"
        "loss_limits:\n  daily_loss_limit_pct: 0.05\n"
        "drawdown:\n  tier_1_pct: 0.2\n  tier_1_factor: 0.25\n"
        "exposure:\n  max_concurrent_positions: 10\n"
        "sizing:\n  method: fractional_kelly\n  kelly_min_sample_size: 50\n"
        "circuit_breaker:\n  confirmation_bars: 5\n"
        "kill_switch:\n  auto_flatten: true\n"
    )
    config = RiskConfig.from_yaml(str(config_file))
    assert config.risk_config_id == "aggressive"
    assert config.version == "2.0.0"
    assert config.daily_loss_limit_pct == 0.05
    assert config.drawdown_tier_1_pct == 0.2
    assert config.drawdown_tier_1_factor == 0.25
    assert config.max_concurrent_positions == 10
    assert config.sizing_method == "fractional_kelly"
    assert config.kelly_min_sample_size == 50
    assert config.circuit_breaker_confirmation_bars == 5
    assert config.kill_switch_auto_flatten is True


def test_config_uses_defaults_for_missing_keys(tmp_path):
    config_file = tmp_path / "partial.yaml"
    config_file.write_text("loss_limits:\n  daily_loss_limit_pct: 0.01\n")
    config = RiskConfig.from_yaml(str(config_file))
    assert config.daily_loss_limit_pct == 0.01
    assert config.weekly_loss_limit_pct == 0.08  # default, unset in file
    assert config.max_concurrent_positions == 5  # default, unset in file
