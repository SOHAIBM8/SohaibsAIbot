"""
Rate limiting on the login endpoint (spec section 24). Deliberately
NOT a reuse of `core/ingestion/rate_limiter.py`'s `RateLimiter` — that
class blocks/sleeps the caller until budget is available, correct for
an outbound exchange call that must eventually succeed. An inbound
login attempt needs the opposite semantics: reject immediately (429)
once the window's budget is spent, never make an attacker's request
hang. Different enough semantics that sharing the class would mean
bending one of the two use cases to fit the other — a real, separate
small class is the right call here, not unreused duplication.

Simple in-process fixed-window counter per client IP (rule 8: build
the simple correct version first) — good enough for a single-operator
V1 with no horizontal scaling; revisit only if this API ever runs as
more than one process.
"""

import threading
import time
from collections import defaultdict


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, client_key: str) -> bool:
        """Returns True if this attempt is allowed (and records it).
        Returns False without recording anything further if the
        window's budget is already spent — never lets a rejected
        attempt itself count as extra pressure beyond the limit."""
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[client_key]
            cutoff = now - self.window_seconds
            attempts[:] = [t for t in attempts if t > cutoff]
            if len(attempts) >= self.max_attempts:
                return False
            attempts.append(now)
            return True

    def reset(self, client_key: str) -> None:
        """Called on a SUCCESSFUL login — a legitimate operator who
        mistyped a few times shouldn't stay throttled after finally
        getting it right."""
        with self._lock:
            self._attempts.pop(client_key, None)
