"""
Error taxonomy for exchange calls (spec section 4.3). Adapters raise
these, not raw HTTP exceptions, so RetryPolicy never has to guess
whether a given failure is worth retrying — that classification
happens once, at the adapter boundary, where the actual status code
is known.
"""


class IngestionError(Exception):
    """Base class for all ingestion-component errors."""


class RetryableIngestionError(IngestionError):
    """Timeout, 5xx, or 429 — transient, safe to retry with backoff."""


class FatalIngestionError(IngestionError):
    """400, invalid symbol, malformed response shape — retrying won't
    help; the caller should stop and record the failure."""
