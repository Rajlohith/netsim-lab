"""Pydantic models of the HTTP API.

REQUEST models validate the SHAPE of what clients send (types, no unknown fields); value ranges and every cross-field rule are enforced
by app.services.validation, which does not depend on pydantic and is the single source of truth for limits.
RESPONSE models describe what the service returns. They are attached to the routes through `responses=` so that they appear in the
OpenAPI document; the routes return the service's plain dicts, so a documentation model can never turn a good result into a 500."""
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictFloat, StrictStr

Num = Union[StrictInt, StrictFloat]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveIn(_In):
    at_s: Num = Field(description="seconds after the start of the run at which the mobile node moves")
    interface: StrictStr = Field(description="mobile-node interface it moves to (see GET /api/scenarios for the topology's interfaces: eth-a, eth-b)")


class ConditionsIn(_In):
    latency_scale: Optional[Num] = Field(None, description="multiplies every link delay; (0, 100]")
    loss_percent: Optional[Num] = Field(None, description="independent random loss per direction on both access links; [0, 100)")
    ha_distance_ms: Optional[Num] = Field(None, description="extra one-way delay on every link that touches the Home Agent; [0, 5000]")
    registration_loss_percent: Optional[Num] = Field(None, description="extra loss of Mobile IP registration messages only; [0, 100)")
    ha_processing_ms: Optional[Num] = Field(None, description="Home Agent registration processing time; [0, 5000]")


class ExperimentRequest(_In):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"approach": "mip-mbb", "scenario": "lossy-access", "seed": 7, "label": "mbb under 2% loss", "predict": True},
        {"approach": "tcp-reconnect", "duration_s": 12, "moves": [{"at_s": 4.0, "interface": "eth-b"}, {"at_s": 8.0, "interface": "eth-a"}],
         "conditions": {"latency_scale": 2.0}}]})
    approach: StrictStr = Field(description="one of GET /api/approaches")
    scenario: Optional[StrictStr] = Field(None, description="one of GET /api/scenarios (default: baseline); explicit fields below override it")
    environment: Optional[StrictStr] = Field(None, description="'emulated' (default) or 'loopback'; 'live' is never available through the API")
    handoff_mode: Optional[StrictStr] = Field(None, description="'bbm' (default) or 'mbb' (only approaches that support make-before-break)")
    duration_s: Optional[Num] = None
    rate_pps: Optional[Num] = None
    payload: Optional[StrictInt] = None
    seed: Optional[StrictInt] = Field(None, description="seed of the random streams; the same configuration and seed reproduce the same result")
    initial_iface: Optional[StrictStr] = None
    moves: Optional[List[MoveIn]] = None
    tcp_detect: Optional[StrictStr] = Field(None, description="tcp-reconnect only: 'event' or 'timeout'")
    old_link_hold_ms: Optional[Num] = None
    detect_delay_ms: Optional[Num] = None
    conditions: Optional[ConditionsIn] = None
    predict: Optional[StrictBool] = Field(None, description="also compute Stage 6 handoff predictions for the finished run (in the worker)")
    prediction_model: Optional[StrictStr] = Field(None, description="one of GET /api/models (default: the first)")
    prediction_algorithm: Optional[StrictStr] = Field(None, description="threshold_baseline | logreg | random_forest (default random_forest)")
    label: Optional[StrictStr] = Field(None, description="free text, at most 80 characters")


# ---- documentation-only response models ---------------------------------------------------------------------------------
class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class Issue(_Out):
    field: Optional[str] = None
    code: str
    message: str


class ErrorBody(_Out):
    code: str
    message: str
    request_id: Optional[str] = None
    issues: List[Issue] = []
    context: Dict[str, Any] = {}


class ErrorResponse(_Out):
    error: ErrorBody


class Progress(_Out):
    phase: str
    step: Optional[int] = None
    steps_total: Optional[int] = None
    fraction: Optional[float] = Field(None, description="null while running: the simulator reports no finer progress than the phase")
    queue_position: Optional[int] = None
    note: Optional[str] = None


class ExperimentOut(_Out):
    id: str
    status: str = Field(description="queued | running | completed | failed | cancelled | incomplete")
    approach: str
    scenario: str
    environment: str
    label: Optional[str] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    configuration: Dict[str, Any]
    resolved_spec: Dict[str, Any]
    progress: Progress
    cancel_requested: bool
    error: Optional[Dict[str, Any]] = None
    partial_outputs: bool
    result: Dict[str, Any]
    prediction: Dict[str, Any]
    data: Dict[str, Any]
    links: Dict[str, str]
    warnings: Optional[List[Dict[str, Any]]] = None


class ExperimentListOut(_Out):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class ValidationReportOut(_Out):
    valid: bool
    issues: List[Issue]
    warnings: List[Dict[str, Any]]
    resolved: Optional[Dict[str, Any]] = None


class TransitionsOut(_Out):
    experiment_id: str
    status: str
    terminal: bool
    items: List[Dict[str, Any]]
    next_after_seq: int


class ResultOut(_Out):
    experiment_id: str
    status: str
    approach: str
    scenario: str
    environment: str
    provenance: Optional[Dict[str, Any]] = None


class MetricsOut(ResultOut):
    metrics: Dict[str, Any]
    source_file: str
    notes: List[str]


class HandoffsOut(ResultOut):
    count: int
    handoffs: List[Dict[str, Any]]
    summary: Optional[Dict[str, Any]] = None
    unavailable_reason: Optional[str] = None


class EventsOut(ResultOut):
    events: List[Dict[str, Any]]
    next_after_seq: Optional[int] = None
    total: int
    clock_note: str


class PredictionsOut(ResultOut):
    model: Dict[str, Any]
    summary: Dict[str, Any]
    decision_points: Dict[str, Any]
    is_live: bool
    caveat: Optional[str] = None


class ModelsOut(_Out):
    models: List[Dict[str, Any]]


class ModelEvaluationOut(_Out):
    model: Dict[str, Any]
    data_origin: Optional[Any] = None
    is_live: bool
    held_out_test_metrics: Dict[str, Any]
    reference: Optional[Dict[str, Any]] = None
    full_results: Optional[Dict[str, Any]] = None
    interpretation_note: str


class ApproachesOut(_Out):
    approaches: List[Dict[str, Any]]


class ScenariosOut(_Out):
    scenarios: List[Dict[str, Any]]


class TopologyOut(_Out):
    name: str
    source_file: str
    nodes: List[Dict[str, Any]]
    links: List[Dict[str, Any]]
    routes: List[Dict[str, Any]]
    notes: List[str]


class CapabilitiesOut(_Out):
    environments: Dict[str, Any]
    limits: Dict[str, Any]
    live_updates: Dict[str, Any]
    request_fields: List[str]


class StatusOut(_Out):
    api_version: str
    job_manager_running: bool
    counts: Dict[str, int]
    active_job_ids: List[str]
