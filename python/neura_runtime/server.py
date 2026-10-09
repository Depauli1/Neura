"""Authenticated, budgeted HTTP surface for the managed runtime.

The CLI owns the loopback socket. This module defines the common response
surface that the notebook bridge will eventually share (not implemented yet).
"""

from __future__ import annotations

import hmac
import json

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from neura_core.api import API_VERSION, error, ok
from neura_runtime.executor import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    BusyError,
    Executor,
    RuntimeStoppingError,
)

MAX_REQUEST_BYTES = 1024 * 1024


class _JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        # User output can contain lone Unicode surrogates. JSON-escape them
        # rather than failing UTF-8 encoding after the cell already executed.
        return json.dumps(
            content, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")


class _TransportMiddleware:
    """Authenticate before routing/body parsing, then cap incoming payloads.

    This includes unknown paths and disabled docs routes: no unauthenticated
    HTTP surface. Read at most 1 MiB before handing JSON to FastAPI/Pydantic.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.encode("utf-8"), self.token
        ):
            response = _JSONResponse(
                error("Unauthorized", "valid Bearer token required"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        if scope["method"] == "POST" and scope["path"] == "/eval":
            media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                response = _JSONResponse(
                    error("UnsupportedMediaType", "Content-Type must be application/json"),
                    status_code=415,
                )
                await response(scope, receive, send)
                return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                response = _JSONResponse(
                    error("PayloadTooLarge", f"request body exceeds {MAX_REQUEST_BYTES} bytes"),
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay_body():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)


class EvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(strict=True, max_length=MAX_REQUEST_BYTES)
    timeout_s: float = Field(
        default=DEFAULT_TIMEOUT_S, strict=True, gt=0, le=MAX_TIMEOUT_S, allow_inf_nan=False
    )
    cwd: str | None = Field(default=None, strict=True, max_length=4096)


def create_app(token: str, executor: Executor) -> FastAPI:
    if not token:
        raise ValueError("token must not be empty")
    app = FastAPI(
        title="neura_runtime",
        version=API_VERSION,
        default_response_class=_JSONResponse,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_TransportMiddleware, token=token)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc: RequestValidationError):
        # Don't echo arbitrary code or non-JSON values (NaN/Infinity) in errors.
        details = [
            {"loc": item["loc"], "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return _JSONResponse(
            error("ValidationError", "invalid request", details=details), status_code=422
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        return _JSONResponse(
            error("HTTPError", str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc: Exception):
        # Uvicorn still logs the underlying exception to stderr; keep the wire
        # response JSON without exposing code, tokens, or server internals.
        return _JSONResponse(
            error("InternalServerError", "internal runtime error"), status_code=500
        )

    @app.get("/health")
    def health() -> dict:
        busy = executor.busy
        return ok(status="busy" if busy else "ready", busy=busy)

    @app.post("/eval")
    def eval_code(payload: EvalRequest):
        try:
            return executor.eval(payload.code, timeout_s=payload.timeout_s, cwd=payload.cwd)
        except BusyError as exc:
            return _JSONResponse(error("BusyError", str(exc)), status_code=409)
        except RuntimeStoppingError as exc:
            return _JSONResponse(error("RuntimeStoppingError", str(exc)), status_code=503)

    @app.post("/interrupt")
    def interrupt() -> dict:
        return ok(interrupted=executor.interrupt())

    return app
