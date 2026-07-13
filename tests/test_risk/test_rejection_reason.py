from core.risk.rejection_reason import RejectionReason, ThrottleReason


def test_rejection_reason_values_are_unique():
    values = [r.value for r in RejectionReason]
    assert len(values) == len(set(values))


def test_throttle_reason_values_are_unique():
    values = [t.value for t in ThrottleReason]
    assert len(values) == len(set(values))


def test_rejection_reason_and_throttle_reason_vocabularies_do_not_overlap():
    # A throttle only reduces size; it must never double as a rejection
    # reason, or callers could conflate "smaller" with "vetoed."
    rejection_values = {r.value for r in RejectionReason}
    throttle_values = {t.value for t in ThrottleReason}
    assert rejection_values.isdisjoint(throttle_values)


def test_expected_rejection_reasons_present():
    names = {r.name for r in RejectionReason}
    assert names == {
        "KILL_SWITCH_ACTIVE",
        "CIRCUIT_BREAKER_ACTIVE",
        "MAX_DAILY_LOSS_REACHED",
        "MAX_WEEKLY_LOSS_REACHED",
        "MAX_DRAWDOWN_REACHED",
        "DATA_QUALITY_FAILED",
        "EXPOSURE_LIMIT_EXCEEDED",
        "MAX_OPEN_POSITIONS",
        "CORRELATION_LIMIT",
        "POSITION_SIZE_TOO_SMALL",
        "INSUFFICIENT_SAMPLE_FOR_KELLY",
    }


def test_expected_throttle_reasons_present():
    names = {t.name for t in ThrottleReason}
    assert names == {
        "DRAWDOWN_TIER_REDUCTION",
        "LOW_KELLY_CONFIDENCE",
        "ELEVATED_VOLATILITY",
    }
