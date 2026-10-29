"""Stage 7 experiment campaigns: a reproducible, resumable set of replicated comparison experiments.

This EXTENDS the Stage 5 interface (it calls compare.runner.run_experiment with ExperimentSpec objects; no second runner).

DESIGN
  * approach configurations are discovered from the approach registry (approaches.APPROACHES / VARIANTS);
  * conditions: one BASELINE plus one-factor-at-a-time sweeps (latency, loss, Home Agent distance, movement-detection delay,
    dwell time between handoffs, registration loss, Home Agent processing time, make-before-break hold). Interactions between
    factors are NOT estimated;
  * replicate k of every (condition, approach) cell uses the SAME handoff schedule (drawn from SeedSequence([base_seed, k]))
    and the same loss seed: common random numbers, which is what makes the later comparison a matched-pairs design;
  * the schedule is randomised per replicate (first move time and dwell jitter), so replicates differ in where the handoff falls
    relative to the probe grid and to the other timers. Without that, a deterministic emulator would return identical replicates.

PERSISTENCE (all under the campaign directory)
  campaign.json   configuration, its hash, software versions, plan summary      plan.json    every planned run with its full spec
  ledger.jsonl    append-only start/end records (survives crashes; a start without an end = incomplete run)
  runs/<scenario>/<run>/   the raw files of each run (Stage 3-5 formats) + experiment.json + campaign_run.json
  _attempts/      earlier failed/incomplete attempts of a run, moved aside (never deleted)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from ..netem.conditions import NetworkConditions
from ..traffic.clock import iso_utc, wall_ns
from .approaches import APPROACHES, SUPPORT, approach_configs
from .experiment import ENVIRONMENTS, ExperimentSpec
from .runner import TUNNELLED, check, run_experiment, software_versions

SCHEMA_VERSION = 1
HA_FACTORS = ("ha_distance_ms", "registration_loss_percent", "ha_processing_ms")
MBB_FACTORS = ("old_link_hold_ms",)
FACTORS = ("latency_scale", "loss_percent", "ha_distance_ms", "detect_delay_ms", "dwell_s", "registration_loss_percent",
           "ha_processing_ms", "old_link_hold_ms")
DEFAULT_SCHEDULE = {"first_move_s": [3.5, 4.5], "dwell_s": 3.0, "dwell_jitter_s": 0.25}


class CampaignError(Exception):
    pass


@dataclass
class CampaignConfig:
    name: str = "campaign"
    base_seed: int = 20261028
    replicates: int = 20
    duration_s: float = 12.0
    rate_pps: float = 100.0
    payload: int = 64
    environment: str = "emulated"
    approaches: object = "auto"                       # "auto" = every registered approach, or a list of ids
    schedule: dict = field(default_factory=lambda: dict(DEFAULT_SCHEDULE))
    baseline: dict = field(default_factory=dict)      # factor -> baseline value (defaults: the ExperimentSpec defaults)
    sweeps: dict = field(default_factory=dict)        # factor -> list of NON-baseline levels
    topology: str | None = None                       # path relative to the project root (default: spec default)

    def __post_init__(self):
        self.schedule = {**DEFAULT_SCHEDULE, **(self.schedule or {})}

    def validate(self):
        if not isinstance(self.replicates, int) or self.replicates < 1:
            raise CampaignError(f"replicates must be an integer >= 1, got {self.replicates!r}")
        if self.environment not in ENVIRONMENTS:
            raise CampaignError(f"unknown environment {self.environment!r}")
        if self.approaches != "auto":
            if not isinstance(self.approaches, list) or not self.approaches:
                raise CampaignError("approaches must be 'auto' or a non-empty list")
            unknown = [a for a in self.approaches if a not in APPROACHES]
            if unknown:
                raise CampaignError(f"unknown approach(es) {unknown}; known: {sorted(APPROACHES)}")
        for f in list(self.sweeps) + list(self.baseline):
            if f not in FACTORS:
                raise CampaignError(f"unknown factor {f!r}; known: {list(FACTORS)}")
        for f, lv in self.sweeps.items():
            if not isinstance(lv, list) or not lv:
                raise CampaignError(f"sweep {f!r} must be a non-empty list of levels")
        lo, hi = self.schedule["first_move_s"]
        if not (0 < lo <= hi):
            raise CampaignError("schedule.first_move_s must be [lo, hi] with 0 < lo <= hi")
        last = hi + max([self.schedule["dwell_s"]] + [v for v in self.sweeps.get("dwell_s", [])]) + self.schedule["dwell_jitter_s"]
        if last >= self.duration_s - 1.0:
            raise CampaignError(f"the latest possible second move ({last:g}s) leaves < 1 s before the end of the run ({self.duration_s:g}s)")

    def to_json(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "name": self.name, "base_seed": self.base_seed, "replicates": self.replicates,
                "duration_s": self.duration_s, "rate_pps": self.rate_pps, "payload": self.payload, "environment": self.environment,
                "approaches": self.approaches, "schedule": self.schedule, "baseline": self.baseline, "sweeps": self.sweeps,
                "topology": self.topology}

    @classmethod
    def from_json(cls, d: dict) -> "CampaignConfig":
        d = dict(d)
        d.pop("schema_version", None)
        known = {"name", "base_seed", "replicates", "duration_s", "rate_pps", "payload", "environment", "approaches", "schedule",
                 "baseline", "sweeps", "topology"}
        extra = set(d) - known
        if extra:
            raise CampaignError(f"unknown campaign field(s) {sorted(extra)}")
        c = cls(**d)
        c.validate()
        return c

    @classmethod
    def load(cls, path) -> "CampaignConfig":
        import yaml
        text = Path(path).read_text()
        return cls.from_json(yaml.safe_load(text) if str(path).endswith((".yaml", ".yml")) else json.loads(text))

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()


@dataclass
class Condition:
    cid: str
    factor: str            # "baseline" or the swept factor
    level: object
    index: int


@dataclass
class PlannedRun:
    key: str                          # scenario_id/run_id, unique
    scenario_id: str
    run_id: str
    condition: str
    factor: str
    level: object
    config_id: str
    approach: str
    variant: str
    replicate: int
    pair_key: str                     # same for the runs that form one matched pair/group (condition + replicate)
    spec: ExperimentSpec
    schedule: dict

    def meta(self, cfg: CampaignConfig) -> dict:
        return {"campaign": cfg.name, "key": self.key, "scenario_id": self.scenario_id, "run_id": self.run_id, "condition": self.condition, "factor": self.factor, "level": self.level,
                "config_id": self.config_id, "approach": self.approach, "variant": self.variant, "replicate": self.replicate,
                "pair_key": self.pair_key, "run_seed": self.spec.seed, "condition_seed": self.spec.conditions.seed,
                "schedule": self.schedule}


@dataclass
class CellNote:
    """A (condition, approach configuration) cell for which NO replicates are executed, with the reason (never silent)."""
    condition: str
    factor: str
    level: object
    config_id: str
    approach: str
    status: str                       # unsupported | unavailable | invalid | not_applicable
    reason: str


@dataclass
class Plan:
    config: CampaignConfig
    conditions: list
    runs: list
    cells: list

    def summary(self) -> dict:
        return {"conditions": len(self.conditions), "approach_configs": len({r.config_id for r in self.runs} | {c.config_id for c in self.cells}),
                "planned_runs": len(self.runs), "not_executed_cells": len(self.cells),
                "not_executed_by_status": _count(c.status for c in self.cells)}


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


# ---- design ---------------------------------------------------------------------------------------------------------------
def build_conditions(cfg: CampaignConfig) -> list:
    conds = [Condition("baseline", "baseline", None, 0)]
    for f in FACTORS:
        for lv in cfg.sweeps.get(f, []):
            conds.append(Condition(f"{f.replace('_', '-')}-{lv:g}" if isinstance(lv, (int, float)) else f"{f}-{lv}", f, lv, len(conds)))
    ids = [c.cid for c in conds]
    if len(ids) != len(set(ids)):
        raise CampaignError(f"duplicate condition ids {ids}")
    return conds


def replicate_seeds(base_seed: int, k: int) -> tuple:
    """(run_seed, condition/loss seed) for replicate k: independent of approach AND condition (common random numbers)."""
    a = int(np.random.SeedSequence([base_seed, k, 0]).generate_state(1)[0]) & 0x7FFFFFFF
    b = int(np.random.SeedSequence([base_seed, k, 1]).generate_state(1)[0]) & 0x7FFFFFFF
    return a, b


def draw_schedule(cfg: CampaignConfig, k: int, dwell_s: float | None = None) -> tuple:
    """Handoff schedule of replicate k: (moves, description). Moves alternate eth-b, eth-a starting from eth-a."""
    s = cfg.schedule
    rng = np.random.default_rng(np.random.SeedSequence([cfg.base_seed, k, 2]))
    t1 = round(float(rng.uniform(*s["first_move_s"])), 3)
    jit = float(rng.uniform(-s["dwell_jitter_s"], s["dwell_jitter_s"]))
    dwell = s["dwell_s"] if dwell_s is None else dwell_s
    t2 = round(t1 + dwell + jit, 3)
    return ((t1, "eth-b"), (t2, "eth-a")), {"first_move_s": t1, "second_move_s": t2, "dwell_s": round(t2 - t1, 3)}


def _apply_factor(spec: ExperimentSpec, cond: NetworkConditions, factor: str, level):
    kw = {}
    if factor == "latency_scale":
        cond.latency_scale = level
    elif factor == "loss_percent":
        cond.loss_percent = level
    elif factor == "ha_distance_ms":
        cond.ha_distance_ms = level
    elif factor == "registration_loss_percent":
        cond.registration_loss_percent = level
    elif factor == "ha_processing_ms":
        cond.ha_processing_ms = level
    elif factor in ("detect_delay_ms", "old_link_hold_ms"):
        kw[factor] = level
    return kw


def build_plan(cfg: CampaignConfig) -> Plan:
    cfg.validate()
    conds = build_conditions(cfg)
    configs = approach_configs(None if cfg.approaches == "auto" else cfg.approaches)
    base_kw = {"duration_s": cfg.duration_s, "rate_pps": cfg.rate_pps, "payload": cfg.payload, "environment": cfg.environment}
    if cfg.topology:
        from .experiment import ROOT
        base_kw["topology"] = ROOT / cfg.topology
    runs, cells = [], []
    for cond in conds:
        for config_id, approach, variant, overrides in configs:
            scenario_id = f"{cond.cid}__{config_id}"
            # applicability of the swept factor to this approach (not executed when it cannot act)
            why = None
            if cond.factor in HA_FACTORS and approach not in TUNNELLED:
                why = f"{approach} has no Home Agent / Mobile IP registration: {cond.factor} cannot affect it"
            if cond.factor in MBB_FACTORS and approach != "mip-mbb":
                why = f"{approach} does not use a make-before-break old-link hold"
            if why:
                cells.append(CellNote(cond.cid, cond.factor, cond.level, config_id, approach, "not_applicable", why))
                continue
            probe_spec = _make_spec(cfg, base_kw, cond, config_id, approach, variant, overrides, 0, scenario_id)
            st, reason = check(probe_spec)
            if st != "ok":
                cells.append(CellNote(cond.cid, cond.factor, cond.level, config_id, approach, st, reason))
                continue
            for k in range(cfg.replicates):
                spec = probe_spec if k == 0 else _make_spec(cfg, base_kw, cond, config_id, approach, variant, overrides, k, scenario_id)
                moves = spec.moves
                runs.append(PlannedRun(f"{scenario_id}/{spec.run_id}", scenario_id, spec.run_id, cond.cid, cond.factor, cond.level,
                                       config_id, approach, variant, k, f"{cond.cid}/{k}", spec,
                                       draw_schedule(cfg, k, cond.level if cond.factor == "dwell_s" else None)[1]))
    return Plan(cfg, conds, runs, cells)


def _make_spec(cfg, base_kw, cond, config_id, approach, variant, overrides, k, scenario_id) -> ExperimentSpec:
    run_seed, cond_seed = replicate_seeds(cfg.base_seed, k)
    nc = NetworkConditions(seed=cond_seed)
    kw = dict(base_kw)
    kw.update(cfg.baseline and {f: v for f, v in cfg.baseline.items() if f in ("detect_delay_ms", "old_link_hold_ms")})
    for f, v in cfg.baseline.items():
        if f not in ("detect_delay_ms", "old_link_hold_ms", "dwell_s"):
            _apply_factor(None, nc, f, v)
    if cond.factor != "baseline":
        kw.update(_apply_factor(None, nc, cond.factor, cond.level))
    moves, _ = draw_schedule(cfg, k, cond.level if cond.factor == "dwell_s" else (cfg.baseline.get("dwell_s")))
    kw.update(overrides)
    return ExperimentSpec(approach=approach, scenario_id=scenario_id, run_id=f"r{k:03d}", moves=moves, seed=run_seed,
                          variant=variant, conditions=nc, **kw)


# ---- ledger ---------------------------------------------------------------------------------------------------------------
class Ledger:
    """Append-only JSON-lines log, flushed and fsynced per record, so a crash loses at most the record being written."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, rec: dict):
        rec = dict(rec, wall_iso=iso_utc(wall_ns()))
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            import os
            os.fsync(f.fileno())

    def read(self) -> list:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:                       # a torn last line from a crash: ignored, never fatal
                continue
        return out

    def final_status(self) -> dict:
        """key -> last known state: {'status', 'reason', ...}; a key with a start but no end is 'incomplete'."""
        state = {}
        for r in self.read():
            k = r.get("key")
            if r["event"] == "start":
                state[k] = {"status": "incomplete", "reason": "started but no end record (crash, kill or power loss)", "seconds": None}
            elif r["event"] == "end":
                state[k] = {"status": r["status"], "reason": r.get("reason", ""), "seconds": r.get("seconds"),
                            "prediction": r.get("prediction")}
        return state


# ---- execution ------------------------------------------------------------------------------------------------------------
def _write_meta(run_dir: Path, plan_run: PlannedRun, cfg: CampaignConfig):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "campaign_run.json").write_text(json.dumps(plan_run.meta(cfg), indent=2, sort_keys=True))


def _archive_attempt(campaign_dir: Path, run: PlannedRun, ledger: Ledger):
    src = campaign_dir / "runs" / run.scenario_id / run.run_id
    if not src.exists():
        return None
    base = campaign_dir / "_attempts" / run.scenario_id
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base / f"{run.run_id}.attempt{n}").exists():
        n += 1
    dst = base / f"{run.run_id}.attempt{n}"
    shutil.move(str(src), str(dst))
    ledger.append({"event": "archived", "key": run.key, "to": str(dst.relative_to(campaign_dir))})
    return dst


def init_campaign(cfg: CampaignConfig, campaign_dir) -> tuple:
    campaign_dir = Path(campaign_dir)
    campaign_dir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(cfg)
    existing = campaign_dir / "campaign.json"
    if existing.exists():
        prev = json.loads(existing.read_text())
        if prev.get("config_sha256") != cfg.digest():
            raise CampaignError(f"{campaign_dir} already holds a campaign with a different configuration "
                                f"({prev.get('config_sha256', '?')[:12]} vs {cfg.digest()[:12]}); use another directory")
    else:
        existing.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "created_wall_iso": iso_utc(wall_ns()),
                                        "config": cfg.to_json(), "config_sha256": cfg.digest(), "software": software_versions(),
                                        "plan_summary": plan.summary()}, indent=2, sort_keys=True))
    (campaign_dir / "plan.json").write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "runs": [dict(r.meta(cfg), spec=r.spec.to_json()) for r in plan.runs],
        "not_executed_cells": [c.__dict__ for c in plan.cells]}, indent=1, sort_keys=True))
    return plan, Ledger(campaign_dir / "ledger.jsonl")


class _Sigterm:
    """Turn SIGTERM into KeyboardInterrupt (main thread only) so a killed campaign records the interrupted run."""

    def __enter__(self):
        self.prev = None
        if threading.current_thread() is threading.main_thread():
            def h(signum, frame):
                raise KeyboardInterrupt("SIGTERM")
            self.prev = signal.signal(signal.SIGTERM, h)
        return self

    def __exit__(self, *a):
        if self.prev is not None:
            signal.signal(signal.SIGTERM, self.prev)


def execute(cfg: CampaignConfig, campaign_dir, *, retry_failed: bool = False, predict_model=None, progress=None,
            runner=run_experiment, max_runs: int | None = None) -> dict:
    """Run every planned run that has no finished 'ok' record. Returns {'ran','skipped_ok','status_counts','interrupted'}.
    `runner` is injectable for tests (fault injection); `max_runs` stops cleanly after that many NEW runs (for partial campaigns)."""
    campaign_dir = Path(campaign_dir)
    plan, ledger = init_campaign(cfg, campaign_dir)
    runs_dir = campaign_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    state = ledger.final_status()
    ran = skipped = 0
    interrupted = False
    with _Sigterm():
        for i, r in enumerate(plan.runs, 1):
            st = state.get(r.key, {}).get("status")
            run_dir = runs_dir / r.scenario_id / r.run_id
            if st == "ok" and (run_dir / "comparison_metrics.json").exists():
                skipped += 1
                continue
            if st in ("failed", "invalid") and not retry_failed and run_dir.exists():
                skipped += 1                                  # deterministic failure already on record; kept as is
                continue
            if max_runs is not None and ran >= max_runs:
                break
            _archive_attempt(campaign_dir, r, ledger)         # incomplete/interrupted/failed earlier attempt: moved aside, never deleted
            ledger.append({"event": "start", "key": r.key})
            t0 = time.time()
            pred_info = None
            try:
                res = runner(r.spec, runs_dir)
                status, reason = res.status, res.reason
                if status == "ok" and predict_model is not None:
                    pred_info = _predict(res.run_dir, predict_model)
            except BaseException as e:                         # KeyboardInterrupt, SystemExit, SIGTERM: recorded, then re-raised
                status = "interrupted" if isinstance(e, (KeyboardInterrupt, SystemExit)) else "failed"
                reason = f"{type(e).__name__}: {e}"
                _record_abnormal(run_dir, r, cfg, status, reason, traceback.format_exc())
                ledger.append({"event": "end", "key": r.key, "status": status, "reason": reason, "seconds": round(time.time() - t0, 3)})
                if status == "interrupted":
                    interrupted = True
                    break
                ran += 1
                continue
            _write_meta(run_dir, r, cfg)
            ledger.append({"event": "end", "key": r.key, "status": status, "reason": reason, "seconds": round(time.time() - t0, 3),
                           "prediction": pred_info})
            ran += 1
            if progress:
                progress(i, len(plan.runs), r.key, status)
    final = ledger.final_status()
    return {"ran": ran, "skipped_ok": skipped, "interrupted": interrupted, "planned": len(plan.runs),
            "status_counts": _count(final.get(r.key, {}).get("status", "not_run") for r in plan.runs)}


def _record_abnormal(run_dir: Path, r: PlannedRun, cfg, status, reason, tb):
    """An interrupted or crashed run leaves whatever raw files were already written, plus experiment.json saying what happened."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "experiment.json").write_text(json.dumps({
        "schema_version": 1, "status": status, "reason": reason, "spec": r.spec.to_json(), "generated_wall_iso": iso_utc(wall_ns()),
        "software": software_versions(), "partial_raw_files": sorted(p.name for p in run_dir.iterdir() if p.is_file())}, indent=2, sort_keys=True))
    (run_dir / "traceback.txt").write_text(tb)
    _write_meta(run_dir, r, cfg)


def _predict(run_dir: Path, model_dir) -> dict:
    """Stage 6 predictions for a finished run (replay: each decision only sees observations <= its decision time). A failure of
    the predictor is recorded, never raised: it must not invalidate the measurement."""
    try:
        from ..ml.integration import annotate_run
        s = annotate_run(run_dir, model_dir)
        return {"ok": True, "decision_points": s["decision_points"], "alarms": s["alarms"], "model_version": s["model_version"],
                "run_in_training_split": s["run_in_training_split"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
