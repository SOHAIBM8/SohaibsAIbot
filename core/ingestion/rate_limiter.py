"""
Per-exchange rate limiting (spec 4.2). A simple fixed-window token
bucket: at most `requests_per_window` weight consumed per
`window_seconds`, blocking (sleeping) rather than exceeding the limit.
No premature sophistication here (sliding window, leaky bucket) —
Binance's own limiter is fixed-window, and this is exactly the "build
the simple correct version first" case from rule 8.
"""

import threading
import time
from collections.abc import Callable

from core.ingestion.types import RateLimitConfig


class RateLimiter:
    def __init__(self, config: RateLimitConfig, clock: Callable[[], float] | None = None):
        self.config = config
        self._clock = clock or time.monotonic
        self._sleep = time.sleep
        self._lock = threading.Lock()
        self._window_start = self._clock()
        self._weight_used = 0

    def acquire(self, weight: int | None = None) -> None:
        """Blocks until `weight` (default: config.weight_per_request)
        can be consumed without exceeding the window's budget."""
        weight = weight if weight is not None else self.config.weight_per_request
        with self._lock:
            now = self._clock()
            elapsed = now - self._window_start
            if elapsed >= self.config.window_seconds:
                self._window_start = now
                self._weight_used = 0
                elapsed = 0.0

            if self._weight_used + weight > self.config.requests_per_window:
                wait = self.config.window_seconds - elapsed
                if wait > 0:
                    self._sleep(wait)
                self._window_start = self._clock()
                self._weight_used = 0

            self._weight_used += weight
