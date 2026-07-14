"""
CSRF protection (spec section 24) via the double-submit-cookie pattern:
a second, NON-httpOnly cookie carries a random token the frontend must
read and echo back in an `X-CSRF-Token` header on every mutating
request. A cross-site attacker can trick a browser into SENDING
cookies automatically, but can't READ this cookie's value from another
origin to put it in the header — that's the entire mitigation. Applies
only to state-changing methods; GET/HEAD/OPTIONS never mutate anything
in this API by convention, so they're exempt.
"""

import hmac
import secrets

from fastapi import Request

CSRF_COOKIE_NAME = "dashboard_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_check_required(request: Request) -> bool:
    return request.method.upper() not in _SAFE_METHODS


def validate_csrf(request: Request) -> bool:
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    header_value = request.headers.get(CSRF_HEADER_NAME)
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)
