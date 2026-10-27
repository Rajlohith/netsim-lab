import os

from fastapi import APIRouter

from .. import __version__
from ..config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "app": s.app_name,
        "version": __version__,
        "environment": s.environment,
        "dry_run": s.dry_run,
        "running_as_root": os.geteuid() == 0,  # should always be False
    }
