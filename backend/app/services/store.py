"""SQLite persistence of experiment metadata and the lifecycle log.

Persistence rules (also in docs/API.md):
  * the database is the source of truth for status; every status change is a compare-and-set inside one transaction and appends a
    row to `transitions` (never rewritten, never deleted except together with the experiment);
  * raw experiment outputs are FILES under experiments/<id>/ (the database stores no measurements, only a small summary copied from
    comparison_metrics.json when a run completes);
  * WAL journal, one short-lived connection per operation (safe across threads), BEGIN IMMEDIATE for every write;
  * at most one ACTIVE (queued/running) experiment per configuration fingerprint, enforced by a partial UNIQUE index, not only by
    application code."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import DuplicateActiveExperiment, IdempotencyKeyReused, QueueFull

SCHEMA_VERSION = 1
STATUSES = ("queued", "running", "completed", "failed", "cancelled", "incomplete")
ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled", "incomplete")
ALLOWED_TRANSITIONS = {"queued": {"running", "cancelled", "failed"},
                       "running": {"completed", "failed", "cancelled", "incomplete"}}
UPDATABLE = {"phase", "worker_pid", "worker_start_ticks", "error_code", "error_message", "run_status", "run_dir", "summary_json",
             "prediction_status", "prediction_error", "prediction_summary_json", "cancel_requested", "owner", "partial_outputs"}
_JSON_COLS = {"request_json": "request", "spec_json": "spec", "summary_json": "summary", "prediction_summary_json": "prediction_summary"}

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled','incomplete')),
  approach TEXT NOT NULL, scenario TEXT NOT NULL, environment TEXT NOT NULL, label TEXT,
  request_json TEXT NOT NULL, spec_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL, idempotency_key TEXT, request_hash TEXT NOT NULL,
  predict_model TEXT, predict_algorithm TEXT,
  prediction_status TEXT NOT NULL DEFAULT 'not_requested', prediction_error TEXT, prediction_summary_json TEXT,
  phase TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_pid INTEGER, worker_start_ticks INTEGER, owner TEXT,
  error_code TEXT, error_message TEXT, run_status TEXT, run_dir TEXT, summary_json TEXT, partial_outputs INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_fingerprint ON experiments(fingerprint) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_key ON experiments(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_experiments_status ON experiments(status, created_at);
CREATE INDEX IF NOT EXISTS ix_experiments_created ON experiments(created_at);
CREATE TABLE IF NOT EXISTS transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  at TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_transitions_exp ON transitions(experiment_id, seq);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for col, name in _JSON_COLS.items():
        raw = d.pop(col)
        d[name] = None if raw is None else json.loads(raw)
    d["cancel_requested"] = bool(d["cancel_requested"])
    d["partial_outputs"] = bool(d["partial_outputs"])
    return d


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- connections -----------------------------------------------------------------------------------------------------
    @contextmanager
    def _conn(self, write: bool = False):
        c = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)       # autocommit; transactions are explicit
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            if write:
                c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                if write:
                    c.execute("COMMIT")
            except BaseException:
                if write and c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_DDL)
            cur = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if cur is None:
                c.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(cur[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: database schema version {cur[0]} is not supported by this code ({SCHEMA_VERSION}); no migration exists")
        finally:
            c.close()

    # ---- writes ----------------------------------------------------------------------------------------------------------
    def insert(self, *, exp_id: str, resolved, idempotency_key: str | None, max_queue: int) -> tuple[dict, bool]:
        """Atomically: replay by idempotency key, refuse a duplicate ACTIVE configuration, refuse a full queue, insert as 'queued'.
        Returns (row, created)."""
        now = utcnow()
        try:
            with self._conn(write=True) as c:
                if idempotency_key:
                    old = c.execute("SELECT * FROM experiments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                    if old is not None:
                        if old["request_hash"] != resolved.request_hash:
                            raise IdempotencyKeyReused("this Idempotency-Key was already used with a different request body",
                                                       details={"existing_experiment_id": old["id"]})
                        return _row(old), False
                dup = c.execute("SELECT id, status FROM experiments WHERE fingerprint=? AND status IN ('queued','running')", (resolved.fingerprint,)).fetchone()
                if dup is not None:
                    raise DuplicateActiveExperiment("an identical experiment is already queued or running",
                                                    details={"existing_experiment_id": dup["id"], "existing_status": dup["status"]})
                if c.execute("SELECT COUNT(*) FROM experiments WHERE status='queued'").fetchone()[0] >= max_queue:
                    raise QueueFull(f"the queue is full ({max_queue} experiments waiting); retry later", headers={"Retry-After": "30"})
                p = resolved.predict
                c.execute("""INSERT INTO experiments (id, created_at, updated_at, status, approach, scenario, environment, label, request_json, spec_json,
                             fingerprint, idempotency_key, request_hash, predict_model, predict_algorithm, prediction_status)
                             VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (exp_id, now, now, resolved.approach, resolved.scenario, resolved.environment, resolved.label,
                           json.dumps(resolved.request, sort_keys=True), json.dumps(resolved.spec.to_json(), sort_keys=True),
                           resolved.fingerprint, idempotency_key, resolved.request_hash,
                           p["model"] if p else None, p["algorithm"] if p else None, "pending" if p else "not_requested"))
                c.execute("INSERT INTO transitions (experiment_id, at, from_status, to_status, note) VALUES (?,?,NULL,'queued','accepted')", (exp_id, now))
                return _row(c.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()), True
        except sqlite3.IntegrityError:
            # another process won the race on a UNIQUE index between our SELECT and INSERT (the index is the real guard)
            with self._conn() as c:
                dup = c.execute("SELECT id, status FROM experiments WHERE fingerprint=? AND status IN ('queued','running')", (resolved.fingerprint,)).fetchone()
                if dup is not None:
                    raise DuplicateActiveExperiment("an identical experiment is already queued or running",
                                                    details={"existing_experiment_id": dup["id"], "existing_status": dup["status"]}) from None
                if idempotency_key:
                    old = c.execute("SELECT * FROM experiments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                    if old is not None and old["request_hash"] == resolved.request_hash:
                        return _row(old), False
                    if old is not None:
                        raise IdempotencyKeyReused("this Idempotency-Key was already used with a different request body",
                                                   details={"existing_experiment_id": old["id"]}) from None
            raise

    def transition(self, exp_id: str, to: str, *, expect=None, note: str | None = None, **fields) -> bool:
        """Compare-and-set status change. False when the experiment is not in an `expect`ed status (or the move is not allowed)."""
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        now = utcnow()
        with self._conn(write=True) as c:
            cur = c.execute("SELECT status FROM experiments WHERE id=?", (exp_id,)).fetchone()
            if cur is None or (expect is not None and cur["status"] not in set(expect)) or to not in ALLOWED_TRANSITIONS.get(cur["status"], ()):
                return False
            sets = {"status": to, "updated_at": now, **fields}
            if to == "running":
                sets["started_at"] = now
            if to in TERMINAL:
                sets["finished_at"] = now
            c.execute(f"UPDATE experiments SET {', '.join(k + '=?' for k in sets)} WHERE id=? AND status=?", (*sets.values(), exp_id, cur["status"]))
            c.execute("INSERT INTO transitions (experiment_id, at, from_status, to_status, note) VALUES (?,?,?,?,?)", (exp_id, now, cur["status"], to, note))
            return True

    def update(self, exp_id: str, **fields) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        if not fields:
            return
        with self._conn(write=True) as c:
            c.execute(f"UPDATE experiments SET {', '.join(k + '=?' for k in fields)}, updated_at=? WHERE id=?", (*fields.values(), utcnow(), exp_id))

    def claim_next(self, owner: str) -> dict | None:
        """Oldest queued experiment -> running (atomic; two schedulers can never claim the same one)."""
        now = utcnow()
        with self._conn(write=True) as c:
            r = c.execute("SELECT id FROM experiments WHERE status='queued' AND cancel_requested=0 ORDER BY created_at, rowid LIMIT 1").fetchone()
            if r is None:
                return None
            c.execute("UPDATE experiments SET status='running', started_at=?, updated_at=?, owner=?, phase='starting' WHERE id=?", (now, now, owner, r["id"]))
            c.execute("INSERT INTO transitions (experiment_id, at, from_status, to_status, note) VALUES (?,?,'queued','running','worker starting')", (r["id"], now))
            return _row(c.execute("SELECT * FROM experiments WHERE id=?", (r["id"],)).fetchone())

    def request_cancel(self, exp_id: str) -> dict | None:
        """Sets the cancel flag on an ACTIVE experiment; returns the row (None if unknown). Terminal rows are returned unchanged."""
        with self._conn(write=True) as c:
            c.execute("UPDATE experiments SET cancel_requested=1, updated_at=? WHERE id=? AND status IN ('queued','running')", (utcnow(), exp_id))
            return _row(c.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone())

    def delete(self, exp_id: str) -> bool:
        with self._conn(write=True) as c:
            return c.execute("DELETE FROM experiments WHERE id=? AND status IN ('completed','failed','cancelled','incomplete')", (exp_id,)).rowcount == 1

    # ---- reads -----------------------------------------------------------------------------------------------------------
    def get(self, exp_id: str) -> dict | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone())

    def list(self, *, statuses=None, approach=None, scenario=None, environment=None, created_after=None, created_before=None,
             label_contains=None, sort="created_at", descending=True, limit=50, offset=0) -> tuple[list[dict], int]:
        if sort not in ("created_at", "finished_at", "status", "approach"):
            raise ValueError(f"cannot sort by {sort!r}")
        where, args = [], []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        for col, v in (("approach", approach), ("scenario", scenario), ("environment", environment)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        if created_after:
            where.append("created_at>=?")
            args.append(created_after)
        if created_before:
            where.append("created_at<?")
            args.append(created_before)
        if label_contains:
            where.append("INSTR(LOWER(COALESCE(label,'')), LOWER(?))>0")
            args.append(label_contains)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM experiments {w}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM experiments {w} ORDER BY {sort} {'DESC' if descending else 'ASC'}, rowid {'DESC' if descending else 'ASC'} LIMIT ? OFFSET ?",
                             (*args, int(limit), int(offset))).fetchall()
        return [_row(r) for r in rows], total

    def find_active(self, fingerprint: str) -> dict | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM experiments WHERE fingerprint=? AND status IN ('queued','running')", (fingerprint,)).fetchone())

    def queue_position(self, exp_id: str) -> int | None:
        """1 = next to start. None if the experiment is not queued."""
        with self._conn() as c:
            me = c.execute("SELECT rowid AS rid, created_at FROM experiments WHERE id=? AND status='queued'", (exp_id,)).fetchone()
            if me is None:
                return None
            return 1 + c.execute("SELECT COUNT(*) FROM experiments WHERE status='queued' AND (created_at < ? OR (created_at = ? AND rowid < ?))",
                                 (me["created_at"], me["created_at"], me["rid"])).fetchone()[0]

    def by_status(self, *statuses: str) -> list[dict]:
        with self._conn() as c:
            return [_row(r) for r in c.execute(f"SELECT * FROM experiments WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at, rowid", statuses)]

    def counts(self) -> dict:
        with self._conn() as c:
            d = {s: 0 for s in STATUSES}
            d.update({r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM experiments GROUP BY status")})
            return d

    def transitions(self, exp_id: str, after_seq: int = 0, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT seq, at, from_status, to_status, note FROM transitions WHERE experiment_id=? AND seq>? ORDER BY seq LIMIT ?",
                                               (exp_id, int(after_seq), int(limit)))]

    def older_than(self, cutoff_iso: str) -> list[str]:
        with self._conn() as c:
            return [r["id"] for r in c.execute("SELECT id FROM experiments WHERE status IN ('completed','failed','cancelled','incomplete') AND finished_at<?", (cutoff_iso,))]
