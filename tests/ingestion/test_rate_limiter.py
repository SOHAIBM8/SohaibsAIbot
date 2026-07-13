from core.ingestion.rate_limiter import RateLimiter
from core.ingestion.types import RateLimitConfig


def test_acquire_within_budget_does_not_sleep():
    config = RateLimitConfig(requests_per_window=10, window_seconds=60, weight_per_request=1)
    clock = _FakeClock()
    limiter = RateLimiter(config, clock=clock.time)
    limiter._sleep = clock.sleep

    for _ in range(10):
        limiter.acquire()

    assert clock.sleep_calls == []


def test_acquire_over_budget_sleeps_until_window_resets():
    config = RateLimitConfig(requests_per_window=2, window_seconds=60, weight_per_request=1)
    clock = _FakeClock()
    limiter = RateLimiter(config, clock=clock.time)
    limiter._sleep = clock.sleep

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # third call must wait for the window to reset

    assert clock.sleep_calls == [60]


class _FakeClock:
    def __init__(self):
        self._t = 0.0
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._t += seconds
