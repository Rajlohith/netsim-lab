import csv
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services import errors as E
from app.services import exports as X
from app.services.experiments import check_token
from app.services.model_registry import ModelRegistry
from app.services.settings import ServiceSettings
from tests.api_helpers import CRASH, FAST, make_service, script_launcher, tmpdir, wait_done


class Results(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="netsim-api-"))
        cls.svc = make_service(cls.tmp); cls.svc.start()
        v, _ = cls.svc.create({**FAST, "approach": "mip-mbb", "handoff_mode": "mbb", "scenario": "lossy-access", "predict": True, "label": "=HYPERLINK(\"x\")", "seed": 3})
        cls.id = v["id"]
        wait_done(cls.svc, cls.id)
        cls.rd = next((cls.svc.manager.job_dir(cls.id) / "runs").rglob("comparison_metrics.json")).parent
        cls.disk = json.loads((cls.rd / "comparison_metrics.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.svc.stop(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_completed(self):
        self.assertEqual(self.svc.get(self.id)["status"], "completed")

    def test_handoffs_equal_the_runs_own_file(self):
        h = self.svc.handoffs(self.id)
        self.assertEqual(h["handoffs"], self.disk["handoffs"]); self.assertEqual(h["count"], len(self.disk["handoffs"])); self.assertEqual(h["summary"], self.disk["handoff_summary"])
        self.assertGreaterEqual(h["count"], 1)

    def test_every_response_says_it_is_not_live(self):
        m = self.svc.metrics(self.id)
        self.assertFalse(m["provenance"]["is_live"]); self.assertIn("Not live", m["provenance"]["note"])
        self.assertFalse(self.svc.predictions(self.id)["is_live"])
        self.assertFalse(self.svc.get(self.id)["data"]["is_live"])

    def test_events_paginate_by_seq_without_gaps_or_repeats(self):
        got, after, total = [], 0, None
        while True:
            e = self.svc.events(self.id, after, 7)
            got += e["events"]; total = e["total"]
            if e["next_after_seq"] is None:
                break
            after = e["next_after_seq"]
        self.assertEqual(len(got), total); seqs = [x["seq"] for x in got]; self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(total, len((self.rd / "approach_events.csv").read_text().splitlines()) - 1)

    def test_predictions_have_decision_points_and_caveat(self):
        p = self.svc.predictions(self.id, limit=5, offset=2)
        self.assertEqual(len(p["decision_points"]["items"]), 5); self.assertEqual(p["decision_points"]["offset"], 2)
        self.assertIn("simulat", (p["caveat"] or "").lower()); self.assertFalse(p["summary"]["run_in_training_split"] is None)
        row = p["decision_points"]["items"][0]
        self.assertEqual(set(row), set(__import__("app.services.results", fromlist=["x"]).PREDICTION_COLUMNS)); self.assertIsInstance(row["alarm"], bool)

    def test_json_export_contains_metrics_and_predictions_unmodified(self):
        out = self.svc.export(self.id, "json")
        doc = json.loads(out["content"])
        self.assertEqual(doc["metrics"], self.disk); self.assertFalse(doc["is_live"]); self.assertEqual(doc["experiment"]["id"], self.id)
        self.assertEqual(doc["predictions"]["summary"], json.loads((self.rd / "handoff_predictions.json").read_text()))
        self.assertTrue(out["filename"].endswith(".json"))

    def test_csv_exports(self):
        rows = list(csv.DictReader(io.StringIO(self.svc.export(self.id, "csv", "handoffs")["content"].decode())))
        self.assertEqual(len(rows), len(self.disk["handoffs"])); self.assertEqual(rows[0]["experiment_id"], self.id)
        self.assertEqual(float(rows[0]["handoff_latency_ms"]), self.disk["handoffs"][0]["handoff_latency_ms"])
        ev = list(csv.DictReader(io.StringIO(self.svc.export(self.id, "csv", "events")["content"].decode())))
        self.assertEqual(len(ev), self.svc.events(self.id)["total"])
        pr = list(csv.DictReader(io.StringIO(self.svc.export(self.id, "csv", "predictions")["content"].decode())))
        self.assertEqual(len(pr), self.svc.predictions(self.id)["decision_points"]["total"])

    def test_null_stays_empty_not_zero(self):
        self.assertEqual(X.to_csv(["a", "b"], [{"a": None, "b": 0}]).decode().splitlines()[1], ",0")

    def test_csv_formula_injection_is_neutralised(self):
        for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertTrue(X.safe_cell(bad).startswith("'"), bad)
        self.assertEqual(X.safe_cell("plain"), "plain"); self.assertEqual(X.safe_cell(-1.5), -1.5)
        hist = self.svc.export_history("csv")["content"].decode()
        self.assertIn("'=HYPERLINK", hist); self.assertNotIn(",=HYPERLINK", hist)

    def test_zip_export_verified_manifest(self):
        out = self.svc.export(self.id, "zip")
        self.addCleanup(out["path"].unlink)
        with zipfile.ZipFile(out["path"]) as z:
            self.assertIsNone(z.testzip()); names = z.namelist()
            man = json.loads(z.read("EXPORT_MANIFEST.json"))
            self.assertIn("run/comparison_metrics.json", names); self.assertIn("run/handoff_predictions.json", names); self.assertIn("experiment_record.json", names)
            self.assertFalse(man["partial"]); self.assertFalse(man["is_live"]); self.assertEqual(man["data_origin"], "simulated")
            import hashlib
            for f in man["files"]:
                self.assertEqual(hashlib.sha256(z.read(f["path"])).hexdigest(), f["sha256"])
            self.assertTrue(all(".." not in n and not n.startswith("/") for n in names))
            self.assertEqual(z.read("run/comparison_metrics.json"), (self.rd / "comparison_metrics.json").read_bytes())

    def test_history_export_and_filters(self):
        doc = json.loads(self.svc.export_history("json")["content"])
        row = next(r for r in doc["experiments"] if r["experiment_id"] == self.id)
        self.assertEqual(row["handoff_count"], self.disk["handoff_summary"]["count"]); self.assertEqual(row["data_origin"], "simulated")
        self.assertEqual(self.svc.list(statuses=["completed"], approach="mip-mbb")["total"] >= 1, True)
        self.assertEqual(self.svc.list(statuses=["failed"], approach="mip-mbb", label_contains="hyperlink")["total"], 0)
        with self.assertRaises(E.ValidationFailed):
            self.svc.list(statuses=["bogus"])
        with self.assertRaises(E.ValidationFailed):
            self.svc.list(sort="password")
        self.assertEqual(self.svc.list(limit=10**6)["limit"], 500)

    def test_bad_export_parameters(self):
        for args in (("xml",), ("csv", "nope")):
            with self.assertRaises(E.ValidationFailed):
                self.svc.export(self.id, *args)
        with self.assertRaises(E.ValidationFailed):
            self.svc.export_history("zip")

    def test_missing_run_files_are_reported_not_invented(self):
        s = make_service(tmpdir(self)); s.start(); self.addCleanup(s.stop)
        v, _ = s.create({**FAST, "seed": 77}); wait_done(s, v["id"])
        (next((s.manager.job_dir(v["id"]) / "runs").rglob("comparison_metrics.json"))).unlink()
        with self.assertRaises(E.ResultsNotAvailable) as cm:
            s.metrics(v["id"])
        self.assertEqual(cm.exception.code, "results_missing")
        shutil.rmtree(s.manager.job_dir(v["id"]) / "runs")
        with self.assertRaises(E.ResultsNotAvailable):
            s.handoffs(v["id"])

    def test_partial_zip_only_when_asked_and_flagged(self):
        s = make_service(tmpdir(self), launcher=script_launcher(CRASH)); s.start(); self.addCleanup(s.stop)
        v, _ = s.create({**FAST}); wait_done(s, v["id"])
        for fmt in ("json", "csv", "zip"):
            with self.assertRaises(E.ResultsNotAvailable):
                s.export(v["id"], fmt)
        out = s.export(v["id"], "zip", include_partial=True); self.addCleanup(out["path"].unlink)
        man = json.loads(zipfile.ZipFile(out["path"]).read("EXPORT_MANIFEST.json"))
        self.assertTrue(man["partial"]); self.assertIn("not results", man["note"]); self.assertTrue(out["filename"].endswith("-partial.zip"))


class Models(unittest.TestCase):
    def test_listing_and_evaluation_are_read_from_the_bundle(self):
        m = ModelRegistry(ServiceSettings())
        d = m.describe("stage6")
        self.assertTrue(d["available"] and d["manifest_verified"]); self.assertEqual(sorted(d["algorithms"]), ["logreg", "random_forest", "threshold_baseline"])
        ev = m.evaluation("stage6")
        disk = json.loads((Path(m.path("stage6")) / "metrics.json").read_text())
        self.assertEqual(ev["held_out_test_metrics"], disk["models"]); self.assertFalse(ev["is_live"])
        self.assertIsNotNone(ev["full_results"]); self.assertIn("no skill", ev["interpretation_note"])

    def test_tampered_bundle_is_not_usable(self):
        d = tmpdir(self); dst = d / "model"; shutil.copytree(ModelRegistry(ServiceSettings()).path("stage6"), dst)
        m = ModelRegistry(ServiceSettings(model_dirs={"t": dst}))
        self.assertTrue(m.describe("t")["available"])
        mf = json.loads((dst / "MANIFEST.json").read_text()); victim = next(iter(mf["files"]))
        with open(dst / victim, "ab") as f:
            f.write(b"x")
        m2 = ModelRegistry(ServiceSettings(model_dirs={"t": dst}))
        self.assertFalse(m2.describe("t")["available"]); self.assertIn("sha256 mismatch", m2.describe("t")["reason"])
        ok, why = m2.require_usable("t", None); self.assertFalse(ok)
        with self.assertRaises(E.NotFound):
            m2.evaluation("t")

    def test_unknown_model_and_missing_bundle(self):
        m = ModelRegistry(ServiceSettings(model_dirs={"gone": Path("/nonexistent/model")}))
        self.assertFalse(m.describe("gone")["available"])
        with self.assertRaises(E.NotFound):
            m.path("other")


class Auth(unittest.TestCase):
    def test_token_check(self):
        s = ServiceSettings(api_token="s3cret")
        check_token(ServiceSettings(), None)
        check_token(s, "Bearer s3cret"); check_token(s, "bearer s3cret")
        for bad in (None, "", "s3cret", "Bearer wrong", "Basic s3cret", "Bearer "):
            with self.assertRaises(E.Unauthorized, msg=bad) as cm:
                check_token(s, bad)
        self.assertEqual(cm.exception.headers["WWW-Authenticate"], "Bearer")


if __name__ == "__main__":
    unittest.main()
