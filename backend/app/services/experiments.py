"""ExperimentService: everything an API route needs, as plain methods returning plain dicts (JSON-ready).

The routes in app/api only translate HTTP; the rules live here and in the modules it composes (validation, store, jobs, results,
exports, model registry)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import __version__
from . import exports as X
from . import results as R
from .catalog import Catalog
from .errors import NotFound, ResultsNotAvailable, Unauthorized, ValidationFailed
from .jobs import JobManager
from .model_registry import ModelRegistry
from .settings import ServiceSettings
from .store import STATUSES, TERMINAL, Store
from .validation import ALLOWED_FIELDS, validate_request

API_PREFIX = "/api"
_IDEM = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
LIVE_NOTE = ("Experiments run in the simulator's virtual time (or on loopback sockets) and their metrics are produced when the run ends. "
             "There are no live or partial metrics: everything served is read from files after completion.")


def check_token(settings: ServiceSettings, authorization: str | None) -> None:
    """Optional bearer token (NETSIM_API_TOKEN). No token configured = open (the server binds to 127.0.0.1 by default)."""
    if not settings.api_token:
        return
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), settings.api_token.encode()):
        raise Unauthorized("a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})


class ExperimentService:
    def __init__(self, settings: ServiceSettings, store: Store, manager: JobManager, catalog: Catalog, models: ModelRegistry):
        self.settings, self.store, self.manager, self.catalog, self.models = settings, store, manager, catalog, models

    # ---- lifecycle -------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.manager.start()

    def stop(self) -> None:
        self.manager.stop()

    # ---- catalog ---------------------------------------------------------------------------------------------------------
    def approaches(self) -> dict:
        return {"approaches": self.catalog.approaches()}

    def scenarios(self) -> dict:
        return {"scenarios": self.catalog.scenarios()}

    def topology(self) -> dict:
        return self.catalog.topology()

    def capabilities(self) -> dict:
        return self.catalog.capabilities(model_ids=self.models.ids()) | {"request_fields": list(ALLOWED_FIELDS), "api_version": __version__}

    def status(self) -> dict:
        return {"api_version": __version__, "job_manager_running": self.manager.running, "counts": self.store.counts(),
                "active_job_ids": self.manager.active_job_ids(), "max_concurrent_jobs": self.settings.max_concurrent_jobs,
                "max_queue_length": self.settings.max_queue_length, "data_dir": str(self.settings.data_dir)}

    # ---- validate / create -----------------------------------------------------------------------------------------------
    def validate(self, raw: dict) -> dict:
        issues, res = validate_request(raw, self.settings, self.catalog, self.models)
        if res is None:
            return {"valid": False, "issues": [i.to_dict() for i in issues], "warnings": [], "resolved": None}
        dup = self.store.find_active(res.fingerprint)
        return {"valid": True, "issues": [], "warnings": res.warnings,
                "resolved": {"configuration": res.request, "spec": res.spec.to_json(), "support": res.support, "fingerprint": res.fingerprint,
                             "would_conflict_with": None if dup is None else {"experiment_id": dup["id"], "status": dup["status"]}}}

    def create(self, raw: dict, idempotency_key: str | None = None) -> tuple[dict, bool]:
        if idempotency_key is not None and not _IDEM.match(idempotency_key):
            raise ValidationFailed("invalid Idempotency-Key", details=[{"field": "Idempotency-Key", "code": "invalid_header",
                                                                         "message": "Idempotency-Key must be 1-128 characters of A-Z a-z 0-9 . _ : -"}])
        issues, res = validate_request(raw, self.settings, self.catalog, self.models)
        if res is None:
            raise ValidationFailed("the experiment request is not valid", details=[i.to_dict() for i in issues])
        row, created = self.manager.submit(res, idempotency_key)
        view = self.view(row)
        view["warnings"] = res.warnings if created else []
        return view, created

    # ---- reads -----------------------------------------------------------------------------------------------------------
    def _row(self, exp_id: str) -> dict:
        R.check_id(exp_id)
        row = self.store.get(exp_id)
        if row is None:
            raise NotFound(f"no experiment {exp_id!r}")
        return row

    def _links(self, exp_id: str) -> dict:
        b = f"{API_PREFIX}/experiments/{exp_id}"
        return {"self": b, "cancel": f"{b}/cancel", "transitions": f"{b}/transitions", "metrics": f"{b}/metrics", "handoffs": f"{b}/handoffs",
                "events": f"{b}/events", "predictions": f"{b}/predictions", "export": f"{b}/export"}

    def _progress(self, row: dict) -> dict:
        st = row["status"]
        if st == "queued":
            return {"phase": "queued", "step": 0, "steps_total": None, "fraction": None, "queue_position": self.store.queue_position(row["id"]),
                    "note": "waiting for a free worker slot"}
        if st == "running":
            p = R.read_progress(self.manager.job_dir(row["id"])) or {}
            return {"phase": p.get("phase") or row.get("phase") or "starting", "step": p.get("step", 0), "steps_total": p.get("steps_total"), "fraction": None,
                    "queue_position": None,
                    "note": "phase-level progress only: the simulator reports no finer progress, so no percentage is given"}
        return {"phase": {"completed": "done", "failed": "failed", "cancelled": "cancelled", "incomplete": "incomplete"}[st], "step": None, "steps_total": None,
                "fraction": 1.0 if st == "completed" else None, "queue_position": None, "note": None}

    def view(self, row: dict) -> dict:
        s = row["status"]
        pred_req = bool(row["predict_model"])
        return {
            "id": row["id"], "status": s, "approach": row["approach"], "scenario": row["scenario"], "environment": row["environment"], "label": row["label"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
            "configuration": row["request"], "resolved_spec": row["spec"],
            "progress": self._progress(row), "cancel_requested": row["cancel_requested"],
            "error": None if not row["error_code"] else {"code": row["error_code"], "message": row["error_message"]},
            "partial_outputs": row["partial_outputs"],
            "result": {"available": s == "completed", "summary": row["summary"] if s == "completed" else None},
            "prediction": {"requested": pred_req, "status": row["prediction_status"], "model": row["predict_model"], "algorithm": row["predict_algorithm"],
                           "error": row["prediction_error"]},
            "data": {"origin": (row["summary"] or {}).get("data_origin") if row["summary"] else None, "is_live": False, "live_metrics_available": False, "note": LIVE_NOTE},
            "links": self._links(row["id"]),
        }

    def get(self, exp_id: str) -> dict:
        return self.view(self._row(exp_id))

    def list(self, *, statuses=None, approach=None, scenario=None, environment=None, created_after=None, created_before=None, label_contains=None,
             sort="-created_at", limit=50, offset=0) -> dict:
        bad = [s for s in (statuses or []) if s not in STATUSES]
        if bad:
            raise ValidationFailed("unknown status filter", details=[{"field": "status", "code": "invalid_choice", "message": f"unknown status {bad}; known: {list(STATUSES)}"}])
        desc = sort.startswith("-")
        col = sort.lstrip("-+")
        if col not in ("created_at", "finished_at", "status", "approach"):
            raise ValidationFailed("unknown sort field", details=[{"field": "sort", "code": "invalid_choice", "message": "sort must be one of created_at, finished_at, status, approach (prefix - for descending)"}])
        limit = max(1, min(int(limit), self.settings.page_size_max))
        rows, total = self.store.list(statuses=statuses, approach=approach, scenario=scenario, environment=environment, created_after=created_after,
                                      created_before=created_before, label_contains=label_contains, sort=col, descending=desc, limit=limit, offset=max(0, int(offset)))
        items = [{"id": r["id"], "status": r["status"], "approach": r["approach"], "scenario": r["scenario"], "environment": r["environment"], "label": r["label"],
                  "created_at": r["created_at"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                  "error": None if not r["error_code"] else {"code": r["error_code"], "message": r["error_message"]},
                  "prediction_status": r["prediction_status"], "summary": r["summary"] if r["status"] == "completed" else None,
                  "links": {"self": f"{API_PREFIX}/experiments/{r['id']}"}} for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": max(0, int(offset))}

    def transitions(self, exp_id: str, after_seq: int = 0, limit: int = 200) -> dict:
        row = self._row(exp_id)
        items = self.store.transitions(exp_id, after_seq, max(1, min(limit, 1000)))
        return {"experiment_id": exp_id, "status": row["status"], "terminal": row["status"] in TERMINAL, "items": items,
                "next_after_seq": items[-1]["seq"] if items else after_seq,
                "polling_note": "poll with after_seq=<next_after_seq>; stop when terminal is true"}

    def cancel(self, exp_id: str) -> tuple[dict, bool]:
        """(view, done). done=False: cancellation was requested for a running experiment and is in progress."""
        R.check_id(exp_id)
        row = self.manager.cancel(exp_id)
        return self.view(row), row["status"] == "cancelled"

    def delete(self, exp_id: str) -> None:
        self.manager.delete(exp_id)

    # ---- results ---------------------------------------------------------------------------------------------------------
    def _completed(self, exp_id: str):
        row = self._row(exp_id)
        rd = R.run_dir_for(self.manager.job_dir(exp_id), row)
        return row, rd

    def _head(self, row: dict, metrics: dict | None = None) -> dict:
        return {"experiment_id": row["id"], "status": row["status"], "approach": row["approach"], "scenario": row["scenario"], "environment": row["environment"],
                "provenance": R.provenance(metrics) if metrics else None}

    def metrics(self, exp_id: str) -> dict:
        row, rd = self._completed(exp_id)
        m = R.load_metrics(rd)
        return {**self._head(row, m), "metrics": m, "source_file": "comparison_metrics.json",
                "notes": ["null means 'not measurable for this run' (the reason is in 'warnings' / 'handoffs_unavailable_reason'), never zero",
                          "values are read from the run's files after completion; there are no live metrics"]}

    def handoffs(self, exp_id: str) -> dict:
        row, rd = self._completed(exp_id)
        m = R.load_metrics(rd)
        return {**self._head(row, m), "count": len(m.get("handoffs") or []), "handoffs": m.get("handoffs") or [], "summary": m.get("handoff_summary"),
                "unavailable_reason": m.get("handoffs_unavailable_reason"), "source_file": "comparison_metrics.json (handoffs)"}

    def events(self, exp_id: str, after_seq: int = 0, limit: int = 500) -> dict:
        row, rd = self._completed(exp_id)
        got = R.read_approach_events(rd, max(0, after_seq), max(1, min(limit, 5000)))
        if got is None:
            raise ResultsNotAvailable("this run has no approach_events.csv", code="events_not_available")
        page, nxt, total = got
        return {**self._head(row), "events": page, "next_after_seq": nxt, "total": total, "source_file": "approach_events.csv",
                "clock_note": "mono_ns/wall_ns are the run's own clock: virtual time for emulated runs, the real monotonic clock for loopback runs"}

    def predictions(self, exp_id: str, limit: int = 500, offset: int = 0) -> dict:
        row, rd = self._completed(exp_id)
        if row["prediction_status"] != "available":
            raise NotFound(f"experiment {exp_id} has no handoff predictions (prediction status: {row['prediction_status']})", code="predictions_not_available",
                           details={"prediction_status": row["prediction_status"], "reason": row["prediction_error"],
                                    "hint": "predictions are computed by the worker when the experiment is created with \"predict\": true"})
        summary = R.read_json(rd / "handoff_predictions.json")
        rows = R.read_prediction_rows(rd)
        if summary is None or rows is None:
            raise ResultsNotAvailable("the prediction files are missing from the run directory", code="results_missing")
        limit, offset = max(1, min(limit, 5000)), max(0, offset)
        return {**self._head(row), "model": {"id": row["predict_model"], "algorithm": row["predict_algorithm"], "version": summary.get("model_version")},
                "summary": summary, "decision_points": {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset},
                "is_live": False, "caveat": summary.get("provenance"), "source_files": ["handoff_predictions.json", "handoff_predictions.csv"]}

    # ---- models ----------------------------------------------------------------------------------------------------------
    def models_list(self) -> dict:
        return {"models": self.models.list()}

    def model_evaluation(self, model_id: str) -> dict:
        return self.models.evaluation(model_id)

    # ---- export ----------------------------------------------------------------------------------------------------------
    def export(self, exp_id: str, fmt: str, table: str = "handoffs", include_partial: bool = False) -> dict:
        """Returns {"content": bytes | None, "path": Path | None, "media_type", "filename", "cleanup": bool}."""
        row = self._row(exp_id)
        jd = self.manager.job_dir(exp_id)
        if fmt not in ("json", "csv", "zip"):
            raise ValidationFailed("unknown export format", details=[{"field": "format", "code": "invalid_choice", "message": "format must be json, csv or zip"}])
        if fmt == "csv" and table not in ("handoffs", "events", "predictions"):
            raise ValidationFailed("unknown table", details=[{"field": "table", "code": "invalid_choice", "message": "table must be handoffs, events or predictions"}])
        if row["status"] != "completed":
            if not (include_partial and fmt == "zip" and row["status"] in TERMINAL):
                raise ResultsNotAvailable(f"experiment {exp_id} is {row['status']}: only completed experiments can be exported "
                                          "(a partial ZIP of a failed/incomplete/cancelled one is available with format=zip&include_partial=true)",
                                          details={"status": row["status"]})
            rd = next((p.parent for p in sorted((jd / "runs").rglob("experiment.json"))), None) if (jd / "runs").is_dir() else None
            record = self._record(row)
            return self._zip(exp_id, rd, record, partial=True, origin=None)
        row, rd = self._completed(exp_id)
        m = R.load_metrics(rd)
        if fmt == "json":
            preds = None
            if row["prediction_status"] == "available":
                preds = {"summary": R.read_json(rd / "handoff_predictions.json"), "decision_points": R.read_prediction_rows(rd)}
            doc = {"export_schema_version": X.EXPORT_SCHEMA_VERSION, "generated_at": X.now_iso(), "is_live": False, "experiment": self.view(row),
                   "provenance": R.provenance(m), "metrics": m, "predictions": preds}
            return {"content": json.dumps(doc, indent=2, sort_keys=True, default=str).encode(), "path": None, "media_type": "application/json",
                    "filename": f"{exp_id}.json", "cleanup": False}
        if fmt == "csv":
            if table == "handoffs":
                body = X.to_csv(X.HANDOFF_COLUMNS, X.handoff_rows(row, m))
            elif table == "events":
                got = R.read_approach_events(rd, 0, 10**9)
                if got is None:
                    raise ResultsNotAvailable("this run has no approach_events.csv", code="events_not_available")
                body = X.to_csv(X.EVENT_COLUMNS, got[0])
            else:
                rows = R.read_prediction_rows(rd)
                if rows is None:
                    raise NotFound(f"experiment {exp_id} has no handoff predictions", code="predictions_not_available")
                body = X.to_csv(R.PREDICTION_COLUMNS, rows)
            return {"content": body, "path": None, "media_type": "text/csv; charset=utf-8", "filename": f"{exp_id}-{table}.csv", "cleanup": False}
        return self._zip(exp_id, rd, self._record(row), partial=False, origin=(m.get("provenance") or {}).get("data_origin"))

    def _record(self, row: dict) -> dict:
        v = self.view(row)
        v.pop("links", None)
        return v

    def _zip(self, exp_id: str, rd, record: dict, *, partial: bool, origin) -> dict:
        self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{exp_id}-", suffix=".zip", dir=self.settings.tmp_dir)
        import os
        os.close(fd)
        X.build_zip(Path(name), run_dir=rd, record=record, partial=partial, data_origin=origin)
        return {"content": None, "path": Path(name), "media_type": "application/zip", "filename": f"{exp_id}{'-partial' if partial else ''}.zip", "cleanup": True}

    def export_history(self, fmt: str, *, statuses=None, approach=None, scenario=None, environment=None, created_after=None, created_before=None) -> dict:
        if fmt not in ("json", "csv"):
            raise ValidationFailed("unknown export format", details=[{"field": "format", "code": "invalid_choice", "message": "format must be json or csv"}])
        rows, total = self.store.list(statuses=statuses, approach=approach, scenario=scenario, environment=environment, created_after=created_after,
                                      created_before=created_before, sort="created_at", descending=False, limit=10_000, offset=0)
        flat = X.history_rows(rows)
        if fmt == "csv":
            return {"content": X.to_csv(X.HISTORY_COLUMNS, flat), "path": None, "media_type": "text/csv; charset=utf-8", "filename": "experiment-history.csv", "cleanup": False}
        doc = {"export_schema_version": X.EXPORT_SCHEMA_VERSION, "generated_at": X.now_iso(), "total_matching": total, "exported": len(flat), "is_live": False,
               "note": "summary values were copied from each completed experiment's comparison_metrics.json; null = not measured", "experiments": flat}
        return {"content": json.dumps(doc, indent=2, sort_keys=True, default=str).encode(), "path": None, "media_type": "application/json",
                "filename": "experiment-history.json", "cleanup": False}


def create_service(settings: ServiceSettings | None = None, *, launcher=None) -> ExperimentService:
    """Build the service graph (does not start the job manager). Fails fast if the shipped scenario presets are invalid."""
    s = (settings or ServiceSettings.from_env()).validate()
    catalog, models, store = Catalog(s), ModelRegistry(s), Store(s.db_path)
    for sc in catalog.scenario_ids():                      # every preset must be a valid request for at least one approach
        issues, _ = validate_request({"approach": "mip-basic", "scenario": sc}, s, catalog, models)
        if issues:
            from .errors import ConfigurationError
            raise ConfigurationError(f"scenario {sc!r} in {s.scenarios_file} is not a valid experiment: {issues[0].message}")
    return ExperimentService(s, store, JobManager(s, store, launcher=launcher), catalog, models)
