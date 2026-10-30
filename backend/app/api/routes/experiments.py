"""Experiment routes: validate, create (background job), inspect, cancel, delete, results, predictions, exports, history."""
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ...services.experiments import ExperimentService
from .. import schemas as S
from ..deps import get_running_service, get_service, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
ERR = S.ErrorResponse
R401, R404, R409, R422 = ({"model": ERR, "description": d} for d in ("missing/invalid bearer token", "unknown experiment", "conflict with the current state", "validation failed"))


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download(out: dict) -> Response:
    disp = f'attachment; filename="{out["filename"]}"'
    if out["path"] is not None:
        return FileResponse(out["path"], media_type=out["media_type"], filename=out["filename"], background=BackgroundTask(_unlink, out["path"]) if out["cleanup"] else None)
    return Response(content=out["content"], media_type=out["media_type"], headers={"Content-Disposition": disp})


# ---- create / validate -----------------------------------------------------------------------------------------------------
@router.post("/experiments/validate", tags=["experiments"], response_model=None, responses={200: {"model": S.ValidationReportOut}, 401: R401, 422: R422},
             summary="Check an experiment request without running it")
def validate_experiment(body: S.ExperimentRequest, svc: ExperimentService = Depends(get_service)):
    """Always 200 when the body is well-formed JSON of the right shape: `valid` says whether it would be accepted, `issues` lists every problem
    (`field`, `code`, `message`), `warnings` lists non-fatal notes, and `resolved` shows the complete configuration that would run, including
    `would_conflict_with` when an identical experiment is already queued or running."""
    return svc.validate(body.model_dump(exclude_none=True))


@router.post("/experiments", tags=["experiments"], status_code=202, response_model=None,
             responses={202: {"model": S.ExperimentOut, "description": "accepted: queued as a background job"},
                        200: {"model": S.ExperimentOut, "description": "replay of an earlier request with the same Idempotency-Key"},
                        401: R401, 409: R409, 422: R422, 429: {"model": ERR, "description": "the queue is full (Retry-After header)"},
                        503: {"model": ERR, "description": "the job manager is not running"}},
             summary="Start an experiment as a managed background job")
def create_experiment(body: S.ExperimentRequest, response: Response, idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                      svc: ExperimentService = Depends(get_running_service)):
    """Validates the request, then queues it. The experiment runs in a separate worker process; poll `GET /api/experiments/{id}`.
    409 `duplicate_active_experiment` if an identical configuration is already queued or running (the response names it).
    With an `Idempotency-Key` header, repeating the same request returns the original experiment (200) instead of creating a second one."""
    view, created = svc.create(body.model_dump(exclude_none=True), idempotency_key)
    response.status_code = 202 if created else 200
    response.headers["Location"] = view["links"]["self"]
    if not created:
        response.headers["Idempotent-Replayed"] = "true"
    return view


# ---- history / inspect -----------------------------------------------------------------------------------------------------
@router.get("/experiments", tags=["history"], response_model=None, responses={200: {"model": S.ExperimentListOut}, 401: R401, 422: R422},
            summary="Experiment history (filter, sort, paginate)")
def list_experiments(status: Optional[List[str]] = Query(default=None, description="repeatable: queued, running, completed, failed, cancelled, incomplete"),
                     approach: Optional[str] = None, scenario: Optional[str] = None, environment: Optional[str] = None,
                     created_after: Optional[str] = Query(default=None, description="ISO-8601 UTC, inclusive"), created_before: Optional[str] = Query(default=None, description="ISO-8601 UTC, exclusive"),
                     label: Optional[str] = Query(default=None, description="case-insensitive substring of the label"),
                     sort: str = Query(default="-created_at", description="created_at | finished_at | status | approach, '-' prefix = descending"),
                     limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0), svc: ExperimentService = Depends(get_service)):
    return svc.list(statuses=status, approach=approach, scenario=scenario, environment=environment, created_after=created_after, created_before=created_before,
                    label_contains=label, sort=sort, limit=limit, offset=offset)


@router.get("/experiments/{experiment_id}", tags=["experiments"], response_model=None, responses={200: {"model": S.ExperimentOut}, 401: R401, 404: R404},
            summary="Status, configuration, progress and summary of one experiment")
def get_experiment(experiment_id: str, svc: ExperimentService = Depends(get_service)):
    return svc.get(experiment_id)


@router.get("/experiments/{experiment_id}/transitions", tags=["experiments"], response_model=None, responses={200: {"model": S.TransitionsOut}, 401: R401, 404: R404},
            summary="Lifecycle log for reliable polling")
def experiment_transitions(experiment_id: str, after_seq: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000), svc: ExperimentService = Depends(get_service)):
    """Append-only log of status changes. Pass `after_seq=<next_after_seq>` to receive only new entries; stop when `terminal` is true."""
    return svc.transitions(experiment_id, after_seq, limit)


@router.post("/experiments/{experiment_id}/cancel", tags=["experiments"], response_model=None,
             responses={200: {"model": S.ExperimentOut, "description": "cancelled (it had not started)"}, 202: {"model": S.ExperimentOut, "description": "cancellation requested; the worker is being stopped, poll the status"},
                        401: R401, 404: R404, 409: R409},
             summary="Cancel a queued or running experiment")
def cancel_experiment(experiment_id: str, response: Response, svc: ExperimentService = Depends(get_running_service)):
    """queued: cancelled immediately (200). running: the worker process group gets SIGTERM, then SIGKILL after the grace period; the response is 202
    and the status becomes `cancelled` shortly after (poll). An experiment that already finished returns 409 `not_cancellable`."""
    view, done = svc.cancel(experiment_id)
    response.status_code = 200 if done else 202
    return view


@router.delete("/experiments/{experiment_id}", tags=["history"], status_code=204, response_model=None, responses={204: {"description": "deleted"}, 401: R401, 404: R404, 409: R409},
               summary="Delete a finished experiment and its files")
def delete_experiment(experiment_id: str, svc: ExperimentService = Depends(get_running_service)):
    svc.delete(experiment_id)
    return Response(status_code=204)


# ---- results ---------------------------------------------------------------------------------------------------------------
@router.get("/experiments/{experiment_id}/metrics", tags=["results"], response_model=None, responses={200: {"model": S.MetricsOut}, 401: R401, 404: R404, 409: R409},
            summary="Metrics of a completed experiment")
def experiment_metrics(experiment_id: str, svc: ExperimentService = Depends(get_service)):
    """The run's comparison_metrics.json plus provenance. 409 `results_not_available` unless the experiment is `completed`: there are no live or partial metrics."""
    return svc.metrics(experiment_id)


@router.get("/experiments/{experiment_id}/handoffs", tags=["results"], response_model=None, responses={200: {"model": S.HandoffsOut}, 401: R401, 404: R404, 409: R409},
            summary="Per-handoff metrics of a completed experiment")
def experiment_handoffs(experiment_id: str, svc: ExperimentService = Depends(get_service)):
    return svc.handoffs(experiment_id)


@router.get("/experiments/{experiment_id}/events", tags=["results"], response_model=None, responses={200: {"model": S.EventsOut}, 401: R401, 404: R404, 409: R409},
            summary="Raw approach event log (paginated by seq)")
def experiment_events(experiment_id: str, after_seq: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=5000), svc: ExperimentService = Depends(get_service)):
    return svc.events(experiment_id, after_seq, limit)


@router.get("/experiments/{experiment_id}/predictions", tags=["predictions"], response_model=None, responses={200: {"model": S.PredictionsOut}, 401: R401, 404: R404, 409: R409},
            summary="Handoff predictions computed for a completed experiment")
def experiment_predictions(experiment_id: str, limit: int = Query(default=500, ge=1, le=5000), offset: int = Query(default=0, ge=0), svc: ExperimentService = Depends(get_service)):
    """Available only when the experiment was created with `predict: true` and the prediction step succeeded (otherwise 404 `predictions_not_available`)."""
    return svc.predictions(experiment_id, limit, offset)


# ---- export ----------------------------------------------------------------------------------------------------------------
@router.get("/experiments/{experiment_id}/export", tags=["export"], response_model=None,
            responses={200: {"description": "the export", "content": {"application/json": {}, "text/csv": {}, "application/zip": {}}}, 401: R401, 404: R404, 409: R409, 422: R422},
            summary="Export one experiment as JSON, CSV or ZIP")
def export_experiment(experiment_id: str, fmt: str = Query(default="json", alias="format", description="json | csv | zip"),
                      table: str = Query(default="handoffs", description="csv only: handoffs | events | predictions"),
                      include_partial: bool = Query(default=False, description="zip only: allow the partial outputs of a failed/incomplete/cancelled experiment"),
                      svc: ExperimentService = Depends(get_service)):
    return _download(svc.export(experiment_id, fmt, table, include_partial))


@router.get("/history/export", tags=["export"], response_model=None,
            responses={200: {"description": "one row per experiment", "content": {"application/json": {}, "text/csv": {}}}, 401: R401, 422: R422},
            summary="Export the experiment history (summary values only)")
def export_history(fmt: str = Query(default="csv", alias="format", description="csv | json"), status: Optional[List[str]] = Query(default=None),
                   approach: Optional[str] = None, scenario: Optional[str] = None, environment: Optional[str] = None,
                   created_after: Optional[str] = None, created_before: Optional[str] = None, svc: ExperimentService = Depends(get_service)):
    return _download(svc.export_history(fmt, statuses=status, approach=approach, scenario=scenario, environment=environment,
                                        created_after=created_after, created_before=created_before))
