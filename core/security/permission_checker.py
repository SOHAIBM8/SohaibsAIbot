"""
ExchangePermissionChecker is the pluggable seam PermissionValidator
depends on — same interface-first pattern as ExchangeAdapter/
ExecutionAdapter. A real Binance implementation
(binance_permission_checker.py) plugs in without PermissionValidator
changing; the standard test suite uses a scripted fake exclusively
(spec section 8), never a real exchange call.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PermissionCheckResult:
    withdrawals_enabled: bool
    raw: dict


class ExchangePermissionChecker(Protocol):
    def check_permissions(self, api_key: str, api_secret: str) -> PermissionCheckResult: ...
