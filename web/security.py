"""Loopback session checks. This is not an account system."""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SESSION_COOKIE = "mutiny_session"
MUTATION_HEADER = "x-mutiny-request"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def error_body(code: str, message: str, *, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def json_error(status: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, retryable=retryable))


def hostname_of(host_header: str) -> str:
    text = (host_header or "").strip()
    if text.startswith("["):
        end = text.find("]")
        return text[: end + 1] if end != -1 else text
    return text.split(":", 1)[0]


def host_is_allowed(host_header: str) -> bool:
    return hostname_of(host_header).lower() in _LOOPBACK


class LocalSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Require the slash so /api.js stays a static module, not an API route.
        if request.url.path.startswith("/api/"):
            if not host_is_allowed(request.headers.get("host", "")):
                return json_error(400, "invalid_host", "The console only accepts loopback hosts.")
            if request.method in {"POST", "PATCH", "DELETE"}:
                origin = request.headers.get("origin", "")
                expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if origin != expected or request.headers.get(MUTATION_HEADER, "") != "1":
                    return json_error(403, "cross_origin", "This request was rejected.")
                content_type = request.headers.get("content-type", "")
                if request.method != "DELETE" and "application/json" not in content_type:
                    return json_error(415, "json_required", "Send a JSON body.")
            if not (request.method == "GET" and request.url.path == "/api/session"):
                cookie = request.cookies.get(SESSION_COOKIE, "")
                expected_token = getattr(request.app.state, "session_token", "")
                if not cookie or cookie != expected_token:
                    return json_error(401, "session_required", "Open the console to start a local session.")
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


def origin_matches(request: Request) -> bool:
    parsed = urlparse(request.headers.get("origin", ""))
    return bool(parsed.scheme and parsed.netloc)
