"""
Maps a Binance error response (HTTP status + JSON error code) to a
classification: retryable or not, plus a category label for
observability/logging. Deliberately NOT a second parallel exception
hierarchy alongside core.ingestion.errors' Retryable/FatalIngestionError
(rule 8) — BinanceExecutionAdapter is what turns a classification into
either an exception (network/rate_limit/fatal/auth) or a normal Order
state transition (rejected is a business outcome, not a system fault:
an order Binance rejects for insufficient balance or a live filter
violation is Order -> REJECTED, exactly like a locally-caught
FilterViolation, not something to raise and crash a caller over).

Binance error codes: https://binance-docs.github.io/apidocs/spot/en/#error-codes
"""

from dataclasses import dataclass

# Not exhaustive — the full authoritative list lives in Binance's docs
# above. These are the codes this adapter needs to route correctly;
# anything unrecognized falls through to 'fatal' (never silently
# retried, never silently treated as a routine rejection).
_AUTH_CODES = {
    -1022,  # signature invalid
    -2014,  # bad API key format
    -2015,  # invalid API key, IP, or permissions for action
}
_REJECTED_CODES = {
    -2010,  # NEW_ORDER_REJECTED (e.g. insufficient balance)
    -2011,  # CANCEL_REJECTED
    -1013,  # invalid quantity/price (server-side filter failure)
    -1021,  # timestamp outside recvWindow — a rejected request, not a network fault
}
_RATE_LIMIT_CODES = {-1003}  # TOO_MANY_REQUESTS


@dataclass
class ExchangeErrorClassification:
    retryable: bool
    category: str  # 'network' | 'rate_limit' | 'rejected' | 'fatal' | 'auth'
    binance_code: int
    message: str


class BinanceErrorClassifier:
    @staticmethod
    def classify(
        status_code: int, binance_code: int | None, message: str
    ) -> ExchangeErrorClassification:
        code = binance_code if binance_code is not None else 0

        if status_code in (429, 418) or code in _RATE_LIMIT_CODES:
            return ExchangeErrorClassification(
                retryable=True, category="rate_limit", binance_code=code, message=message
            )
        if status_code >= 500:
            return ExchangeErrorClassification(
                retryable=True, category="network", binance_code=code, message=message
            )
        if code in _AUTH_CODES:
            return ExchangeErrorClassification(
                retryable=False, category="auth", binance_code=code, message=message
            )
        if code in _REJECTED_CODES:
            return ExchangeErrorClassification(
                retryable=False, category="rejected", binance_code=code, message=message
            )
        # Any other 4xx (including an unrecognized code) is fatal —
        # never silently retried, never silently treated as a routine
        # rejection either. An unknown failure mode should be loud.
        return ExchangeErrorClassification(
            retryable=False, category="fatal", binance_code=code, message=message
        )

    @staticmethod
    def classify_network_error(message: str) -> ExchangeErrorClassification:
        """For a raw connection error/timeout with no HTTP response at
        all — always retryable, no Binance code available. Distinct
        from `classify()` because there is no status_code/body to
        inspect; this is the pure "we don't know what happened" case."""
        return ExchangeErrorClassification(
            retryable=True, category="network", binance_code=0, message=message
        )
