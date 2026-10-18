"""HTTP Basic auth middleware + security headers."""
from __future__ import annotations

import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self' https://cdn.plaid.com; frame-src https://cdn.plaid.com; "
        "connect-src 'self' https://production.plaid.com https://sandbox.plaid.com; "
        "frame-ancestors 'none'"
    ),
}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str, password: str):
        super().__init__(app)
        self._user = user.encode()
        self._pass = password.encode()

    async def dispatch(self, request, call_next):
        response: Response = await call_next(request) if self._authorized(request) else self._deny()
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        # Revalidate every asset so a deploy is picked up without a hard refresh;
        # API responses are personal financial data and must never be stored.
        response.headers.setdefault(
            "Cache-Control",
            "no-store" if request.url.path.startswith("/api/") else "no-cache",
        )
        return response

    def _authorized(self, request) -> bool:
        path = request.url.path
        if path in ("/api/health", "/manifest.webmanifest", "/sw.js", "/offline", "/offline.html") or path.startswith("/icons/"):
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return secrets.compare_digest(user.encode(), self._user) and secrets.compare_digest(
            password.encode(), self._pass
        )

    def _deny(self) -> Response:
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="budget"'},
        )
