"""
Every trade rejection carries one of these exact values — never a
free-text guess (spec decision #3). ThrottleReason is a distinct enum
because a throttle only reduces size; it never vetoes a trade to zero
on its own, so mixing the two vocabularies would blur "this trade
didn't happen" with "this trade happened smaller than proposed."
"""

from enum import Enum


class RejectionReason(Enum):
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    CIRCUIT_BREAKER_ACTIVE = "circuit_breaker_active"
    MAX_DAILY_LOSS_REACHED = "max_daily_loss_reached"
    MAX_WEEKLY_LOSS_REACHED = "max_weekly_loss_reached"
    MAX_DRAWDOWN_REACHED = "max_drawdown_reached"
    DATA_QUALITY_FAILED = "data_quality_failed"
    EXPOSURE_LIMIT_EXCEEDED = "exposure_limit_exceeded"
    MAX_OPEN_POSITIONS = "max_open_positions"
    CORRELATION_LIMIT = "correlation_limit"
    POSITION_SIZE_TOO_SMALL = "position_size_too_small"
    INSUFFICIENT_SAMPLE_FOR_KELLY = "insufficient_sample_for_kelly"


class ThrottleReason(Enum):
    """A throttle REDUCES size; it never vetoes the trade to zero on
    its own — that's what RejectionReason is for."""

    DRAWDOWN_TIER_REDUCTION = "drawdown_tier_reduction"
    LOW_KELLY_CONFIDENCE = "low_kelly_confidence"
    ELEVATED_VOLATILITY = "elevated_volatility"
