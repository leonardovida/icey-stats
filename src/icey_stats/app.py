"""HTTP applications: the public ingest API and the loopback-only admin API.

The two are separate ASGI apps served on separate sockets. The admin app is never mounted on the
public listener, and its listener binds 127.0.0.1 inside the container only.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .db import CapacityError, Database, QueryRejected
from .validation import ValidationError, validate_github, validate_hit, validate_report

log = logging.getLogger("icey_stats.http")

REPORT_MAX_BYTES = 4 * 1024
HIT_MAX_BYTES = 1024
GITHUB_MAX_BYTES = 1024 * 1024
QUERY_MAX_BYTES = 64 * 1024

# Bounds how many threads may wait on the database at once; writes are serialized by the DB lock.
_db_limiter = anyio.CapacityLimiter(4)


class HTTPError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray):
        return value.hex()
    return str(value)


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=_json_default, separators=(",", ":")).encode()


def _no_content(headers: dict[str, str] | None = None) -> Response:
    return Response(status_code=204, headers={"Cache-Control": "no-store", **(headers or {})})


async def _db(func: Callable[..., Any], *args: Any) -> Any:
    return await anyio.to_thread.run_sync(func, *args, limiter=_db_limiter)


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def read_body(request: Request, limit: int) -> bytes:
    """Read the body, refusing anything over `limit` bytes before it is buffered or parsed."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise HTTPError(400, "invalid content-length")
        if int(declared) > limit:
            raise HTTPError(413, "body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPError(413, "body too large")
    return bytes(body)


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HTTPError(400, "body is not valid JSON") from exc


def _handler(func: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def wrapped(request: Request) -> Response:
        try:
            return await func(request)
        except HTTPError as exc:
            return JSON({"error": exc.message}, status_code=exc.status, headers=exc.headers)
        except ValidationError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        except QueryRejected as exc:
            return JSON({"error": str(exc)}, status_code=400)
        except CapacityError:
            return JSON({"error": "try again tomorrow"}, status_code=429)

    return wrapped


class RequestLogMiddleware:
    """Logs failed and slow requests: method, path, status, duration. Never addresses or headers."""

    def __init__(self, app: ASGIApp, listener: str) -> None:
        self.app = app
        self.listener = listener

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error", extra={"fields": {"listener": self.listener, "path": scope["path"][:100]}})
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if status >= 400 or duration_ms > 1000:
                log.info(
                    "request",
                    extra={
                        "fields": {
                            "listener": self.listener,
                            "method": scope["method"],
                            "path": scope["path"][:100],
                            "status": status,
                            "duration_ms": duration_ms,
                        }
                    },
                )


# ------------------------------------------------------------------------------------------ public


def build_public_app(db: Database, settings: Settings) -> ASGIApp:
    token = settings.ingest_token.encode()
    allowed_origin = settings.cors_origin

    def cors(request: Request) -> dict[str, str]:
        """CORS for the website counter: one origin, no credentials. A foreign Origin is refused."""
        origin = request.headers.get("origin")
        if origin is None:
            return {"Vary": "Origin"}
        if origin != allowed_origin:
            raise HTTPError(403, "origin not allowed", {"Vary": "Origin"})
        return {"Access-Control-Allow-Origin": allowed_origin, "Vary": "Origin"}

    @_handler
    async def report(request: Request) -> Response:
        if _media_type(request) != "application/json":
            raise HTTPError(415, "use application/json")
        data = parse_json(await read_body(request, REPORT_MAX_BYTES))
        await _db(db.insert_report, validate_report(data))
        return _no_content()

    @_handler
    async def hit(request: Request) -> Response:
        if request.method == "OPTIONS":
            if request.headers.get("origin") != allowed_origin:
                raise HTTPError(403, "origin not allowed", {"Vary": "Origin"})
            return _no_content(
                {
                    **cors(request),
                    "Access-Control-Allow-Methods": "POST, GET",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "86400",
                }
            )
        headers = cors(request)
        if request.method == "GET":
            data: Any = dict(request.query_params)
        else:
            # navigator.sendBeacon with a string body sends text/plain, which needs no preflight.
            if _media_type(request) not in {"application/json", "text/plain"}:
                raise HTTPError(415, "use application/json or text/plain", headers)
            data = parse_json(await read_body(request, HIT_MAX_BYTES))
        try:
            parsed = validate_hit(data)
        except ValidationError as exc:
            raise HTTPError(400, str(exc), headers) from exc
        await _db(db.record_hit, parsed)
        return _no_content(headers)

    @_handler
    async def github(request: Request) -> Response:
        # Authenticate before reading the body, so anonymous clients cannot make the service buffer 1 MB.
        scheme, _, provided = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(provided.strip().encode(), token):
            raise HTTPError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        if _media_type(request) != "application/json":
            raise HTTPError(415, "use application/json")
        data = parse_json(await read_body(request, GITHUB_MAX_BYTES))
        snapshot = validate_github(data, expected_repository=settings.github_repository)
        await _db(db.ingest_github, snapshot, data)
        return _no_content()

    async def healthz(request: Request) -> Response:
        try:
            ok = await _db(db.healthy)
        except Exception:
            log.exception("health check failed")
            ok = False
        return PlainTextResponse("ok" if ok else "unavailable", status_code=200 if ok else 503,
                                 headers={"Cache-Control": "no-store"})

    app = Starlette(
        routes=[
            Route("/v1/report", report, methods=["POST"]),
            Route("/v1/hit", hit, methods=["POST", "GET", "OPTIONS"]),
            Route("/v1/github", github, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET", "HEAD"]),
        ]
    )
    return RequestLogMiddleware(app, "public")


# ------------------------------------------------------------------------------------------- admin


def build_admin_app(db: Database) -> ASGIApp:
    @_handler
    async def summary(request: Request) -> Response:
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError as exc:
            raise HTTPError(400, "days must be an integer") from exc
        return JSON(await _db(db.summary, days))

    @_handler
    async def query(request: Request) -> Response:
        body = await read_body(request, QUERY_MAX_BYTES)
        if _media_type(request) == "application/json":
            data = parse_json(body)
            sql = data.get("sql") if isinstance(data, dict) else None
        else:
            try:
                sql = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPError(400, "query is not UTF-8") from exc
        return JSON(await _db(db.run_readonly_query, sql))

    @_handler
    async def backup(request: Request) -> Response:
        path = await _db(db.backup)
        return JSON({"backup": path})

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok" if await _db(db.healthy) else "unavailable")

    app = Starlette(
        routes=[
            Route("/summary", summary, methods=["GET"]),
            Route("/query", query, methods=["POST"]),
            Route("/backup", backup, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )
    return RequestLogMiddleware(app, "admin")
