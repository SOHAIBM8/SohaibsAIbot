"""
Structured logging setup. Every module gets a logger via
structlog.get_logger(__name__) and logs with key=value context
(logger.info("feature_computed", feature="ema_20", rows=1500)) instead
of interpolated strings — this is what makes logs queryable later once
they're shipped somewhere like Loki or CloudWatch, instead of being
grep-only text.
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(),
    )
