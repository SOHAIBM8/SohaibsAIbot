"""
Exponential backoff with jitter (spec 4.3). Retries only
RetryableIngestionError; FatalIngestionError and any other exception
propagate immediately — retrying a 400 or a malformed response can
never succeed, so there's no point burning attempts on it.
"""

import random
import time
from collections.abc import Callable
from typing import TypeVar

import structlog

from core.ingestion.errors import RetryableIngestionError

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 5,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
        sleep: Callable[[float], None] | None = None,
        rand: Callable[[], float] | None = None,
    ):
        self.max_retries = max_retries
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._sleep = sleep or time.sleep
        self._rand = rand or random.random

    def execute(self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return fn()
            except RetryableIngestionError as exc:
                if attempt >= self.max_retries:
                    logger.error("retry_exhausted", attempts=attempt + 1, error=str(exc))
                    raise
                delay = min(self.base_delay_seconds * (2**attempt), self.max_delay_seconds)
                delay *= 0.5 + self._rand()  # jitter: 0.5x-1.5x
                logger.warning(
                    "retrying_after_error", attempt=attempt + 1, delay_seconds=delay, error=str(exc)
                )
                self._sleep(delay)
                attempt += 1
