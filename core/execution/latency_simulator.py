"""
Configurable fixed + jittered delay, standing in for real network/
exchange-processing latency in paper trading. Deliberately NOT a
queueing-theoretic model — this project doesn't operate at a timescale
where that fidelity pays for itself yet (rule 8: simple correct version
first). Jitter is injected via `rand` (same pattern as
core/ingestion/retry_policy.py's backoff jitter) so tests are
deterministic without patching the global `random` module.
"""

import random
from collections.abc import Callable


class LatencySimulator:
    def __init__(self, base_ms: float, jitter_ms: float, rand: Callable[[], float] | None = None):
        self.base_ms = base_ms
        self.jitter_ms = jitter_ms
        self._rand = rand or random.random

    def delay(self) -> float:
        """Returns a simulated latency in ms: base_ms + a uniform
        random amount in [0, jitter_ms). Never negative — latency
        can't be negative, so jitter only ever adds, never subtracts."""
        return self.base_ms + self._rand() * self.jitter_ms
