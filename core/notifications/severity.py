"""
Fixed event_type -> severity mapping for the dashboard's Notifications
feed (docs/dashboard_ui_spec.md section 18: "Severity is inherited
directly from the backend's own event definitions — the frontend does
not invent its own severity taxonomy"). None of the event dataclasses
in core/risk/events.py or core/security/events.py carry a severity
field themselves, so this is the one place that mapping is declared —
not fabricated ad hoc per caller, and not left for the frontend to
invent. core/security/events.py's own module docstring states
CredentialValidationFailed "carries the same severity as
KillSwitchEngaged downstream" — honored literally here, not
reinterpreted.
"""

NOTIFICATION_EVENT_TYPES = [
    "KillSwitchEngaged",
    "KillSwitchDisengaged",
    "CredentialValidationFailed",
    "EmergencyRevocationTriggered",
    "DrawdownTierChanged",
    "DailyLossLimitBreached",
    "CircuitBreakerTripped",
    "CircuitBreakerCleared",
    "ArmingExpired",
    "TradeSignalGenerated",
]

SEVERITY_BY_EVENT_TYPE = {
    "KillSwitchEngaged": "critical",
    "CredentialValidationFailed": "critical",  # same severity as KillSwitchEngaged, per
    # core/security/events.py's own docstring
    "EmergencyRevocationTriggered": "critical",
    "DrawdownTierChanged": "warning",
    "DailyLossLimitBreached": "warning",
    "CircuitBreakerTripped": "warning",
    "KillSwitchDisengaged": "info",
    "CircuitBreakerCleared": "info",
    "ArmingExpired": "info",
    # Informational, not a hazard alert — a signal is an observation
    # the scanner made, never an order it placed (signal-only mode).
    "TradeSignalGenerated": "info",
}
