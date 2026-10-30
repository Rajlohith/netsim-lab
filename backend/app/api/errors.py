"""One error envelope for every failure:

    {"error": {"code": "validation_failed", "message": "...", "request_id": "...",
               "issues": [{"field": "duration_s", "code": "out_of_range", "message": "..."}],     # per-field problems (may be empty)
               "context": {"existing_experiment_id": "exp-..."}}}                                   # extra facts (may be empty)
"""
import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..services.errors import ServiceError

log = logging.getLogger("netsim.api")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def error_body(code: str, message: str, request_id=None, issues=None, context=None) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id, "issues": issues or [], "context": context or {}}}


def _rid(request: Request):
    return getattr(request.state, "request_id", None)


def _pydantic_issue(err: dict) -> dict:
    loc = [str(x) for x in err.get("loc", ())]
    if loc and loc[0] in ("body", "query", "path", "header"):
        loc = loc[1:]
    t = err.get("type", "")
    code = ("required" if t == "missing" else "unknown_field" if t == "extra_forbidden" else "invalid_json" if t == "json_invalid"
            else "out_of_range" if t.startswith(("greater_than", "less_than", "string_too", "too_short", "too_long")) else "invalid_type")
    return {"field": ".".join(loc) or None, "code": code, "message": err.get("msg", "invalid value")}      # never echo err['input']


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID", "")
        rid = rid if _RID.match(rid) else uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        issues = exc.details if isinstance(exc.details, list) else []
        context = exc.details if isinstance(exc.details, dict) else {}
        return JSONResponse(status_code=exc.http_status, content=error_body(exc.code, exc.message, _rid(request), issues, context), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=error_body("validation_failed", "the request is not valid", _rid(request), [_pydantic_issue(e) for e in exc.errors()]))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        msg = "no such route" if exc.status_code == 404 and exc.detail == "Not Found" else str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content=error_body(code, msg, _rid(request)), headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error (request %s)", _rid(request))
        return JSONResponse(status_code=500, content=error_body("internal_error", "an internal error occurred; quote the request_id when reporting it", _rid(request)))
