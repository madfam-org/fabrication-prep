"""Error bodies: ``{"errors": [{"code", "message", "path"?, "details"?}], "request_id"}``.

Validation failures that concern one request field carry ``path`` (a JSON pointer into the request body)
and, for range violations, ``details`` naming the key, the bound and the offending value, so the caller
(pravara's dispatcher) can surface the exact reason.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import DatabaseUnavailable
from .logging import describe_db_error

log = logging.getLogger(__name__)


@dataclass
class Problem:
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.details is not None:
            out["details"] = self.details
        return out


@dataclass
class ApiError(Exception):
    status: int
    problems: list[Problem] = field(default_factory=list)
    headers: dict[str, str] | None = None

    @classmethod
    def one(cls, status: int, code: str, message: str, path: str | None = None, headers=None) -> ApiError:
        return cls(status, [Problem(code, message, path)], headers)


def bad_request(code: str, message: str, path: str | None = None) -> ApiError:
    return ApiError.one(400, code, message, path)


def not_found(message: str = "The resource does not exist or is not visible to the caller") -> ApiError:
    return ApiError.one(404, "not_found", message)


def unauthorized(code: str, message: str) -> ApiError:
    return ApiError.one(401, code, message, headers={"WWW-Authenticate": 'Bearer realm="fabrication-prep"'})


def forbidden(code: str, message: str) -> ApiError:
    return ApiError.one(403, code, message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError.one(409, code, message)


def unprocessable(problems: list[Problem]) -> ApiError:
    return ApiError(422, problems)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def body(problems: list[Problem], request_id: str) -> dict:
    return {"errors": [p.as_dict() for p in problems], "request_id": request_id}


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(body(exc.problems, _request_id(request)), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors()[:20]:
            loc = [str(p) for p in err.get("loc", ()) if p != "body"]
            problems.append(Problem("invalid_request", err.get("msg", "invalid"), "/" + "/".join(loc)))
        return JSONResponse(body(problems, _request_id(request)), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            body([Problem(code, message)], _request_id(request)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        # Never echo or log the driver's message: it can quote parameters or connection details.
        log.error("database error on %s %s: %s", request.method, request.url.path, describe_db_error(exc))
        status = 503 if isinstance(exc, psycopg.OperationalError) else 500
        problem = Problem("database_error", "The request failed in the database layer; it was logged without detail")
        return JSONResponse(body([problem], _request_id(request)), status_code=status)

    @app.exception_handler(DatabaseUnavailable)
    async def _db_unavailable(request: Request, exc: DatabaseUnavailable) -> JSONResponse:
        log.error("database unavailable on %s %s", request.method, request.url.path)
        return JSONResponse(
            body([Problem("database_unavailable", "The database is not available")], _request_id(request)),
            status_code=503,
        )
