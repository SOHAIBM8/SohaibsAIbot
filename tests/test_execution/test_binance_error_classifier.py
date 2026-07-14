"""
Pure logic, no network — covers the full retryable/rejected/fatal/auth/
rate_limit taxonomy the spec's testing strategy calls for.
"""

from core.execution.binance_error_classifier import BinanceErrorClassifier


def test_429_is_rate_limit_and_retryable():
    result = BinanceErrorClassifier.classify(429, None, "Too many requests")
    assert result.category == "rate_limit"
    assert result.retryable is True


def test_418_is_rate_limit_and_retryable():
    # Binance uses 418 for an IP ban after repeated 429s.
    result = BinanceErrorClassifier.classify(418, None, "IP banned")
    assert result.category == "rate_limit"
    assert result.retryable is True


def test_binance_rate_limit_code_is_rate_limit_even_without_429_status():
    result = BinanceErrorClassifier.classify(400, -1003, "Too many requests")
    assert result.category == "rate_limit"
    assert result.retryable is True


def test_5xx_is_network_and_retryable():
    result = BinanceErrorClassifier.classify(503, None, "Service unavailable")
    assert result.category == "network"
    assert result.retryable is True


def test_bad_signature_is_auth_and_not_retryable():
    result = BinanceErrorClassifier.classify(400, -1022, "Signature invalid")
    assert result.category == "auth"
    assert result.retryable is False


def test_bad_api_key_format_is_auth_and_not_retryable():
    result = BinanceErrorClassifier.classify(401, -2014, "API-key format invalid")
    assert result.category == "auth"
    assert result.retryable is False


def test_invalid_api_key_permissions_is_auth_and_not_retryable():
    result = BinanceErrorClassifier.classify(401, -2015, "Invalid API-key, IP, or permissions")
    assert result.category == "auth"
    assert result.retryable is False


def test_insufficient_balance_is_rejected_and_not_retryable():
    result = BinanceErrorClassifier.classify(400, -2010, "Account has insufficient balance")
    assert result.category == "rejected"
    assert result.retryable is False


def test_cancel_rejected_is_rejected_and_not_retryable():
    result = BinanceErrorClassifier.classify(400, -2011, "Unknown order sent")
    assert result.category == "rejected"
    assert result.retryable is False


def test_filter_failure_is_rejected_and_not_retryable():
    result = BinanceErrorClassifier.classify(400, -1013, "Filter failure: LOT_SIZE")
    assert result.category == "rejected"
    assert result.retryable is False


def test_recv_window_exceeded_is_rejected_and_not_retryable():
    result = BinanceErrorClassifier.classify(400, -1021, "Timestamp outside recvWindow")
    assert result.category == "rejected"
    assert result.retryable is False


def test_unrecognized_code_is_fatal_not_silently_retried_or_rejected():
    result = BinanceErrorClassifier.classify(400, -9999, "Some new error Binance introduced")
    assert result.category == "fatal"
    assert result.retryable is False


def test_no_code_at_all_is_fatal():
    result = BinanceErrorClassifier.classify(400, None, "Malformed response")
    assert result.category == "fatal"
    assert result.retryable is False


def test_classify_network_error_is_always_network_and_retryable():
    result = BinanceErrorClassifier.classify_network_error("connection reset")
    assert result.category == "network"
    assert result.retryable is True
    assert result.binance_code == 0


def test_classification_preserves_the_original_message():
    result = BinanceErrorClassifier.classify(400, -2010, "Account has insufficient balance")
    assert result.message == "Account has insufficient balance"
    assert result.binance_code == -2010
