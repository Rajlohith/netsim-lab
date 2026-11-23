"""Read-only catalogue routes: approaches, scenarios, capabilities, status."""
from fastapi import APIRouter, Depends

from ...services.experiments import ExperimentService
from .. import schemas as S
from ..deps import get_service, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
_E = {401: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse}}


@router.get("/approaches", tags=["catalog"], response_model=None, responses={200: {"model": S.ApproachesOut}, 401: _E[401]},
            summary="List the supported mobility approaches")
def list_approaches(svc: ExperimentService = Depends(get_service)):
    """Every approach of the registry with what it is and is NOT, where it can run, and whether it can run on THIS server
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
    """Read from the same YAML the experiments use (loader); nothing is generated."""
    return svc.topology()


@router.get("/capabilities", tags=["catalog"], response_model=None, responses={200: {"model": S.CapabilitiesOut}, 401: _E[401]},
            summary="Limits, accepted request fields and how live updates work")
def capabilities(svc: ExperimentService = Depends(get_service)):
    return svc.capabilities()


@router.get("/status", tags=["catalog"], response_model=None, responses={200: {"model": S.StatusOut}, 401: _E[401]},
            summary="Job manager state and experiment counts per status")
def service_status(svc: ExperimentService = Depends(get_service)):
    return svc.status()
