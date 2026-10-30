"""FastAPI dependencies. The service object lives on app.state.service (built at start-up by the lifespan handler)."""
from typing import Optional

from fastapi import Header, Request

from ..services.errors import ServiceUnavailable
from ..services.experiments import ExperimentService, check_token


def get_service(request: Request) -> ExperimentService:
    svc = getattr(request.app.state, "service", None)
    if svc is None:
        raise ServiceUnavailable("the experiment service has not been started")
    return svc


def get_running_service(request: Request) -> ExperimentService:
    """For operations that need the job manager (create/cancel/delete): a queued experiment would never run otherwise."""
    svc = get_service(request)
    if not svc.manager.running:
        raise ServiceUnavailable("the job manager is not running; experiments cannot be accepted")
    return svc


def require_auth(request: Request, authorization: Optional[str] = Header(default=None)) -> None:
    svc = get_service(request)
    check_token(svc.settings, authorization)
