from core.execution.latency_simulator import LatencySimulator


def test_zero_jitter_roll_returns_base_ms():
    sim = LatencySimulator(base_ms=100.0, jitter_ms=50.0, rand=lambda: 0.0)
    assert sim.delay() == 100.0


def test_max_jitter_roll_returns_base_plus_full_jitter():
    sim = LatencySimulator(base_ms=100.0, jitter_ms=50.0, rand=lambda: 1.0)
    assert sim.delay() == 150.0


def test_mid_jitter_roll_is_proportional():
    sim = LatencySimulator(base_ms=100.0, jitter_ms=50.0, rand=lambda: 0.5)
    assert sim.delay() == 125.0


def test_delay_never_falls_below_base_ms():
    sim = LatencySimulator(base_ms=20.0, jitter_ms=10.0, rand=lambda: 0.0)
    for roll in (0.0, 0.25, 0.5, 0.75, 0.999):
        sim._rand = lambda roll=roll: roll
        assert sim.delay() >= 20.0


def test_zero_jitter_config_is_deterministic():
    sim = LatencySimulator(base_ms=42.0, jitter_ms=0.0)
    assert sim.delay() == 42.0
