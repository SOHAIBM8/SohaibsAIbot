"""
Security headers middleware (spec section 24). A strict CSP even
though this service returns JSON, not HTML — defense in depth, and
the frontend's own `index.html` (served separately by Vite/its build
output, not by this API) should set an equally strict CSP itself once
built. `X-Content-Type-Options`/`X-Frame-Options` cost nothing and
close off a class of browser-side misinterpretation attacks even on a
pure JSON API.
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

_CSP = "default-src 'none'; frame-ancestors 'none'"


async def security_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
