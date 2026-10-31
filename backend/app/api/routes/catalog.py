"""Read-only catalogue routes: approaches, scenarios, capabilities, status, prediction models."""
from fastapi import APIRouter, Depends

from ...services.experiments import ExperimentService
from .. import schemas as S
from ..deps import get_service, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
_E = {401: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse}}


@router.get("/approaches", tags=["catalog"], response_model=None, responses={200: {"model": S.ApproachesOut}, 401: _E[401]},
            summary="List the supported mobility approaches")
def list_approaches(svc: ExperimentService = Depends(get_service)):
    """Every approach of the Stage 5 registry with what it is and is NOT, where it can run, and whether it can run on THIS server
    (e.g. `quic-migration` needs aioquic). Environments the API does not expose (`live`) are listed as not exposed."""
    return svc.approaches()


@router.get("/scenarios", tags=["catalog"], response_model=None, responses={200: {"model": S.ScenariosOut}, 401: _E[401]},
            summary="List the available network scenarios")
def list_scenarios(svc: ExperimentService = Depends(get_service)):
    """Named parameter sets (handoff schedule + network conditions), with every resolved parameter value."""
    return svc.scenarios()


@router.get("/topology", tags=["catalog"], response_model=None, responses={200: {"model": S.TopologyOut}, 401: _E[401]},
            summary="The lab topology the experiments run on (nodes, links, routes, configured link impairments)")
def topology(svc: ExperimentService = Depends(get_service)):
    """Read from the same YAML the experiments use (Stage 2 loader); nothing is generated."""
    return svc.topology()


@router.get("/capabilities", tags=["catalog"], response_model=None, responses={200: {"model": S.CapabilitiesOut}, 401: _E[401]},
            summary="Limits, accepted request fields and how live updates work")
def capabilities(svc: ExperimentService = Depends(get_service)):
    return svc.capabilities()


@router.get("/status", tags=["catalog"], response_model=None, responses={200: {"model": S.StatusOut}, 401: _E[401]},
            summary="Job manager state and experiment counts per status")
def service_status(svc: ExperimentService = Depends(get_service)):
    return svc.status()


@router.get("/models", tags=["predictions"], response_model=None, responses={200: {"model": S.ModelsOut}, 401: _E[401]},
            summary="List the trained handoff-prediction model bundles")
def list_models(svc: ExperimentService = Depends(get_service)):
    return svc.models_list()


@router.get("/models/{model_id}/evaluation", tags=["predictions"], response_model=None, responses={200: {"model": S.ModelEvaluationOut}, 401: _E[401], 404: _E[404]},
            summary="Held-out evaluation recorded when the model was trained")
def model_evaluation(model_id: str, svc: ExperimentService = Depends(get_service)):
    """Metrics on the held-out test scenarios, the always-alarm reference and the caveats. Simulated data only."""
    return svc.model_evaluation(model_id)
