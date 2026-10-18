"""HTTP Basic auth middleware + security headers + CSRF/throttle guards."""
from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Only meaningful over HTTPS; harmless on loopback HTTP.
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self' https://cdn.plaid.com; frame-src https://cdn.plaid.com; "
        "connect-src 'self' https://production.plaid.com https://sandbox.plaid.com; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}

# --- brute-force throttle (#3) -------------------------------------------------
# Browsers send cached Basic credentials on cross-site form POSTs and the docs
# recommend exposing the app via a tunnel, where every peer is 127.0.0.1 — so
# the failure counter is GLOBAL, not per-IP. Lockout is a 429 with Retry-After
# (sleeping in the middleware would exhaust the threadpool instead).
FAIL_THRESHOLD = 5
COOLDOWN_BASE_S = 2.0
_COOLDOWN_CAP_S = 300.0

_throttle_lock = threading.Lock()
_throttle = {"count": 0, "last_fail": 0.0}

# --- request size cap for /api/import (#10) -------------------------------------
MAX_BODY_BYTES = 10 * 1024 * 1024

# Public (unauthenticated) paths. /icons/ is exempt only for a SINGLE path
# segment: `/icons/../index.html` must stay behind auth (#7).
_PUBLIC_PATH_RE = re.compile(r"^/icons/[^/]+$")


def _norm_netloc(netloc: str) -> str:
    netloc = (netloc or "").lower()
    if netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    if netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    return netloc


def _mutates(method: str) -> bool:
    return method not in ("GET", "HEAD", "OPTIONS")


def _same_origin(request) -> bool:
    """CSRF guard (#1): browsers attach cached Basic credentials to cross-site
    form POSTs. A cross-site request carries a foreign Origin header or
    Sec-Fetch-Site: cross-site. Non-browser clients send neither and keep working."""
    if not _mutates(request.method):
        return True
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    origin = request.headers.get("Origin", "")
    if not origin:
        return True  # curl / API clients
    try:
        origin_host = _norm_netloc(urlsplit(origin).netloc)
    except ValueError:
        return False
    host_header = request.headers.get("host") or ""
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        host_header = forwarded.split(",")[0].strip()
    return origin_host == _norm_netloc(host_header)


def _cooldown_seconds(failures: int) -> float:
    return min(_COOLDOWN_CAP_S, COOLDOWN_BASE_S * (2 ** max(0, failures - FAIL_THRESHOLD)))


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str, password: str):
        super().__init__(app)
        self._user = user.encode()
        self._pass = password.encode()

    async def dispatch(self, request, call_next):
        # CSRF guard first: never let a cross-site write reach a handler.
        if not _same_origin(request):
            return self._with_headers(
                JSONResponse({"detail": "Cross-site request blocked"}, status_code=403), request)

        # Import size cap (#10): reject before the body is read.
        if request.url.path == "/api/import":
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                return self._with_headers(JSONResponse(
                    {"detail": "Import too large"}, status_code=413), request)

        # Brute-force throttle (#3): while locked out, even correct credentials
        # wait out the cooldown. Public paths bypass the throttle entirely —
        # a lockout must not take public assets down with it.
        if request.url.path != "/api/health" and not _PUBLIC_PATH_RE.fullmatch(request.url.path):
            now = time.monotonic()
            with _throttle_lock:
                if _throttle["count"] >= FAIL_THRESHOLD:
                    remaining = _cooldown_seconds(_throttle["count"]) - (now - _throttle["last_fail"])
                    if remaining > 0:
                        return self._with_headers(JSONResponse(
                            {"detail": "Too many failed attempts"},
                            status_code=429,
                            headers={"Retry-After": str(int(remaining) + 1)},
                        ), request)

        authorized, request_ok = self._authorized(request)
        if not authorized:
            with _throttle_lock:
                if request_ok:  # malformed Authorization header does not count
                    _throttle["count"] += 1
                    _throttle["last_fail"] = time.monotonic()
            return self._with_headers(self._deny(), request)

        with _throttle_lock:
            if _throttle["count"]:
                _throttle["count"] = 0
                _throttle["last_fail"] = 0.0

        response: Response = await call_next(request)
        return self._with_headers(response, request)

    def _with_headers(self, response: Response, request) -> Response:
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        # Revalidate every asset so a deploy is picked up without a hard refresh;
        # API responses are personal financial data and must never be stored.
        response.headers.setdefault(
            "Cache-Control",
            "no-store" if request.url.path.startswith("/api/") else "no-cache",
        )
        return response

    def _authorized(self, request) -> tuple[bool, bool]:
        """Returns (authorized, count_failure)."""
        path = request.url.path
        if path == "/api/health" or _PUBLIC_PATH_RE.fullmatch(path):
            return True, False
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False, True
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False, False
        ok = secrets.compare_digest(user.encode(), self._user) and secrets.compare_digest(
            password.encode(), self._pass
        )
        return ok, True

    def _deny(self) -> Response:
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="budget"'},
        )
