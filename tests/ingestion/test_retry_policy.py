import pytest

from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.ingestion.retry_policy import RetryPolicy


def test_succeeds_without_retry():
    policy = RetryPolicy(sleep=lambda s: None, rand=lambda: 0.0)
    assert policy.execute(lambda: 42) == 42


def test_retries_retryable_error_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableIngestionError("transient")
        return "ok"

    sleeps = []
    policy = RetryPolicy(max_retries=5, sleep=sleeps.append, rand=lambda: 0.0)
    assert policy.execute(flaky) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept before the 2nd and 3rd attempts


def test_exhausts_retries_and_raises():
    policy = RetryPolicy(max_retries=2, sleep=lambda s: None, rand=lambda: 0.0)

    def always_fails():
        raise RetryableIngestionError("nope")

    with pytest.raises(RetryableIngestionError):
        policy.execute(always_fails)


def test_fatal_error_never_retried():
    calls = {"n": 0}

    def fatal():
        calls["n"] += 1
        raise FatalIngestionError("bad request")

    policy = RetryPolicy(max_retries=5, sleep=lambda s: None, rand=lambda: 0.0)
    with pytest.raises(FatalIngestionError):
        policy.execute(fatal)
    assert calls["n"] == 1
