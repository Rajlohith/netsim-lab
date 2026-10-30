"""Result export: JSON document, CSV tables and a ZIP of the raw run files. Pure functions over data already read from the run
directory - no measurement is computed or invented here.

CSV safety: text cells that a spreadsheet could interpret as a formula (leading = + - @ tab CR) are prefixed with a single quote, because
`label` is client-supplied text."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

EXPORT_SCHEMA_VERSION = 1
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
HANDOFF_COLUMNS = ("experiment_id", "approach", "scenario", "environment", "event_id", "detail", "recovered", "handoff_latency_ms", "service_gap_ms",
                   "interruption_ms", "loss_packets", "rtt_before_ms", "rtt_after_ms", "signalling_messages", "signalling_bytes", "retransmits", "failures")
EVENT_COLUMNS = ("seq", "mono_ns", "wall_ns", "approach", "node", "kind", "bytes", "detail")
HISTORY_COLUMNS = ("experiment_id", "status", "approach", "scenario", "environment", "label", "created_at", "started_at", "finished_at", "error_code",
                   "handoff_count", "all_handoffs_recovered", "handoff_latency_ms_mean", "handoff_latency_ms_max", "service_gap_ms_mean", "service_gap_ms_max",
                   "loss_packets_total", "loss_percent", "packets_lost", "packets_sent", "loss_is_exact", "rtt_p50_ms", "goodput_bps",
                   "mobility_overhead_bytes_per_packet", "run_status", "data_origin", "prediction_status")
ZIP_SUFFIXES = (".csv", ".json", ".txt")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v[:1] in FORMULA_PREFIXES:
        return "'" + v
    return v


def to_csv(columns, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([safe_cell(r.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def handoff_rows(row: dict, metrics: dict) -> list[dict]:
    out = []
    for h in metrics.get("handoffs") or []:
        d = {"experiment_id": row["id"], "approach": row["approach"], "scenario": row["scenario"], "environment": row["environment"], **h}
        d["failures"] = "; ".join(map(str, h.get("failures") or []))
        out.append(d)
    return out


def history_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        s = r.get("summary") or {}
        out.append({"experiment_id": r["id"], "status": r["status"], "approach": r["approach"], "scenario": r["scenario"], "environment": r["environment"],
                    "label": r["label"], "created_at": r["created_at"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                    "error_code": r["error_code"], "prediction_status": r["prediction_status"], **{k: s.get(k) for k in HISTORY_COLUMNS if k in s}})
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_zip(dest: Path, *, run_dir: Path | None, record: dict, partial: bool, data_origin: str | None) -> Path:
    """ZIP = run/<raw files of the run directory> + experiment_record.json + EXPORT_MANIFEST.json (sha256 of every member).
    Only top-level .csv/.json/.txt files of the run directory are included (no directory traversal, no symlink targets)."""
    members: list[tuple[str, bytes]] = []
    if run_dir is not None:
        for p in sorted(Path(run_dir).iterdir()):
            if p.is_file() and not p.is_symlink() and p.suffix in ZIP_SUFFIXES:
                members.append((f"run/{p.name}", p.read_bytes()))
    members.append(("experiment_record.json", json.dumps(record, indent=2, sort_keys=True, default=str).encode()))
    manifest = {"export_schema_version": EXPORT_SCHEMA_VERSION, "generated_at": now_iso(), "experiment_id": record.get("id"), "status": record.get("status"),
                "partial": partial, "is_live": False, "data_origin": data_origin,
                "note": ("PARTIAL outputs of an experiment that did not complete: not results, do not analyse them as such." if partial else
                         "Raw files exactly as the run wrote them (see docs/TRAFFIC_MEASUREMENT.md and docs/MOBILITY_COMPARISON.md for the formats)."),
                "files": [{"path": n, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in members]}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in members:
            z.writestr(zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0)), b, zipfile.ZIP_DEFLATED)
        z.writestr(zipfile.ZipInfo("EXPORT_MANIFEST.json", date_time=(1980, 1, 1, 0, 0, 0)), json.dumps(manifest, indent=2, sort_keys=True), zipfile.ZIP_DEFLATED)
    return dest
