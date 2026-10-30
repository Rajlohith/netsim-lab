"""HTTP-level tests of the Stage 8 API (FastAPI TestClient).

These need fastapi + httpx (requirements.txt / requirements-dev.txt). They are SKIPPED when those packages are missing; the same rules are
covered without a web framework by test_api_validation / _store / _jobs / _results, which test the service layer the routes call."""
import json
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE = True
except Exception:                                  # pragma: no cover - environment without fastapi/httpx
    HAVE = False

from tests.api_helpers import CRASH, FAST, SLEEP, make_service, script_launcher, tmpdir, wait_done, wait_for


def client(test, launcher=None, **kw):
    from app.main import create_app
    svc = make_service(tmpdir(test), launcher=launcher, **kw)
    c = TestClient(create_app(svc))
    c.__enter__()                                   # runs the lifespan: starts the job manager
    test.addCleanup(c.__exit__, None, None, None)
    return c, svc


def err(r, status, code):
    assert r.status_code == status, (r.status_code, r.text)
    e = r.json()["error"]
    assert e["code"] == code, e
    assert set(e) == {"code", "message", "request_id", "issues", "context"}, e
    assert e["request_id"] and r.headers["X-Request-ID"] == e["request_id"]
    return e


@unittest.skipUnless(HAVE, "fastapi/httpx not installed")
class Http(unittest.TestCase):
    def test_catalog_endpoints(self):
        c, _ = client(self, script_launcher(SLEEP))
        a = c.get("/api/approaches").json()["approaches"]
        self.assertEqual({x["id"] for x in a}, {"tcp-reconnect", "mip-basic", "mip-mbb", "ro-model", "quic-migration"})
        self.assertIn("baseline", {s["id"] for s in c.get("/api/scenarios").json()["scenarios"]})
        cap = c.get("/api/capabilities").json()
        self.assertFalse(cap["live_updates"]["websocket"]); self.assertEqual(c.get("/api/status").json()["job_manager_running"], True)
        self.assertEqual(c.get("/api/models").json()["models"][0]["id"], "stage6")
        self.assertEqual(c.get("/api/health").status_code, 200)

    def test_validate_endpoint_reports_issues_with_200(self):
        c, _ = client(self, script_launcher(SLEEP))
        r = c.post("/api/experiments/validate", json={"approach": "mip-basic", "environment": "live"})
        self.assertEqual(r.status_code, 200); self.assertFalse(r.json()["valid"]); self.assertEqual(r.json()["issues"][0]["code"], "environment_not_exposed")
        ok = c.post("/api/experiments/validate", json=FAST).json()
        self.assertTrue(ok["valid"]); self.assertEqual(ok["resolved"]["configuration"]["duration_s"], 6.0)

    def test_create_returns_202_location_and_validation_errors_422(self):
        c, svc = client(self, script_launcher(SLEEP))
        r = c.post("/api/experiments", json=FAST)
        self.assertEqual(r.status_code, 202); self.assertEqual(r.headers["Location"], f"/api/experiments/{r.json()['id']}"); self.assertEqual(r.json()["status"], "queued")
        e = err(c.post("/api/experiments", json={"approach": "nope"}), 422, "validation_failed")
        self.assertEqual(e["issues"][0]["code"], "unknown_approach")
        e = err(c.post("/api/experiments", json={"approach": "mip-basic", "topology": "/etc/passwd"}), 422, "validation_failed")
        self.assertEqual(e["issues"][0]["code"], "unknown_field")
        e = err(c.post("/api/experiments", json={"approach": "mip-basic", "duration_s": "ten"}), 422, "validation_failed")
        self.assertEqual(e["issues"][0]["field"], "duration_s")
        err(c.post("/api/experiments", content=b"{not json", headers={"Content-Type": "application/json"}), 422, "validation_failed")
        err(c.post("/api/experiments", json={}), 422, "validation_failed")
        self.assertEqual(svc.store.counts()["queued"] + svc.store.counts()["running"], 1)

    def test_duplicate_is_409_with_context_and_idempotency_replays_200(self):
        c, _ = client(self, script_launcher(SLEEP))
        first = c.post("/api/experiments", json=FAST, headers={"Idempotency-Key": "abc"})
        e = err(c.post("/api/experiments", json=FAST), 409, "duplicate_active_experiment")
        self.assertEqual(e["context"]["existing_experiment_id"], first.json()["id"])
        again = c.post("/api/experiments", json=FAST, headers={"Idempotency-Key": "abc"})
        self.assertEqual((again.status_code, again.headers["Idempotent-Replayed"], again.json()["id"]), (200, "true", first.json()["id"]))
        err(c.post("/api/experiments", json={**FAST, "seed": 9}, headers={"Idempotency-Key": "abc"}), 409, "idempotency_key_reused")
        err(c.post("/api/experiments", json=FAST, headers={"Idempotency-Key": "has space"}), 422, "validation_failed")

    def test_queue_full_is_429_with_retry_after(self):
        c, _ = client(self, script_launcher(SLEEP), max_concurrent_jobs=1, max_queue_length=1)
        c.post("/api/experiments", json={**FAST, "seed": 1}); c.post("/api/experiments", json={**FAST, "seed": 2})
        r = c.post("/api/experiments", json={**FAST, "seed": 3})
        err(r, 429, "queue_full"); self.assertEqual(r.headers["Retry-After"], "30")

    def test_cancel_running_202_then_cancelled_and_not_cancellable_409(self):
        c, svc = client(self, script_launcher(SLEEP))
        i = c.post("/api/experiments", json=FAST).json()["id"]
        wait_for(svc, i, lambda x: x["status"] == "running")
        r = c.post(f"/api/experiments/{i}/cancel"); self.assertEqual(r.status_code, 202); self.assertTrue(r.json()["cancel_requested"])
        wait_done(svc, i, 20)
        self.assertEqual(c.get(f"/api/experiments/{i}").json()["status"], "cancelled")
        err(c.post(f"/api/experiments/{i}/cancel"), 409, "not_cancellable")
        t = c.get(f"/api/experiments/{i}/transitions").json(); self.assertTrue(t["terminal"]); self.assertEqual([x["to_status"] for x in t["items"]], ["queued", "running", "cancelled"])
        self.assertEqual(c.get(f"/api/experiments/{i}/transitions", params={"after_seq": t["next_after_seq"]}).json()["items"], [])

    def test_cancel_queued_is_200(self):
        c, svc = client(self, script_launcher(SLEEP), max_concurrent_jobs=1)
        a = c.post("/api/experiments", json={**FAST, "seed": 1}).json()["id"]; b = c.post("/api/experiments", json={**FAST, "seed": 2}).json()["id"]
        wait_for(svc, a, lambda x: x["status"] == "running")
        r = c.post(f"/api/experiments/{b}/cancel"); self.assertEqual((r.status_code, r.json()["status"]), (200, "cancelled"))

    def test_not_found_and_bad_routes(self):
        c, _ = client(self, script_launcher(SLEEP))
        err(c.get("/api/experiments/exp-0000000000000000"), 404, "not_found")
        err(c.get("/api/experiments/not-an-id"), 404, "not_found")
        err(c.get("/api/nothing"), 404, "not_found")
        err(c.put("/api/experiments"), 405, "method_not_allowed")
        err(c.get("/api/models/zzz/evaluation"), 404, "not_found")

    def test_results_409_until_completed_and_failure_is_visible(self):
        c, svc = client(self, script_launcher(CRASH))
        i = c.post("/api/experiments", json=FAST).json()["id"]; wait_done(svc, i)
        for path in ("metrics", "handoffs", "events", "predictions", "export"):
            e = err(c.get(f"/api/experiments/{i}/{path}"), 409, "results_not_available"); self.assertEqual(e["context"]["status"], "incomplete")
        g = c.get(f"/api/experiments/{i}").json(); self.assertEqual((g["status"], g["error"]["code"]), ("incomplete", "worker_died"))
        r = c.get(f"/api/experiments/{i}/export", params={"format": "zip", "include_partial": "true"}); self.assertEqual(r.status_code, 200); self.assertEqual(r.headers["content-type"], "application/zip")
        err(c.get("/api/experiments", params={"status": "bogus"}), 422, "validation_failed")

    def test_history_list_filter_pagination_delete(self):
        c, svc = client(self, script_launcher(CRASH))
        ids = [c.post("/api/experiments", json={**FAST, "seed": s}).json()["id"] for s in (1, 2, 3)]
        for i in ids:
            wait_done(svc, i)
        r = c.get("/api/experiments", params={"status": ["incomplete"], "limit": 2}).json(); self.assertEqual((r["total"], len(r["items"]), r["limit"]), (3, 2, 2))
        self.assertEqual(c.get("/api/experiments", params={"limit": 0}).status_code, 422)
        self.assertEqual(c.delete(f"/api/experiments/{ids[0]}").status_code, 204); err(c.get(f"/api/experiments/{ids[0]}"), 404, "not_found")
        h = c.get("/api/history/export", params={"format": "csv"}); self.assertEqual(h.status_code, 200); self.assertIn("text/csv", h.headers["content-type"])
        self.assertEqual(len(h.text.strip().splitlines()), 3)

    def test_auth_token_protects_everything_except_health(self):
        c, _ = client(self, script_launcher(SLEEP), api_token="tok")
        err(c.get("/api/approaches"), 401, "unauthorized"); err(c.post("/api/experiments", json=FAST), 401, "unauthorized")
        self.assertEqual(c.get("/api/approaches", headers={"Authorization": "Bearer tok"}).status_code, 200)
        self.assertEqual(c.get("/api/health").status_code, 200)

    def test_service_not_started_is_503(self):
        from app.main import create_app
        svc = make_service(tmpdir(self))
        c = TestClient(create_app(svc))                 # no lifespan: manager not running
        err(c.post("/api/experiments", json=FAST), 503, "service_unavailable")
        self.assertEqual(c.get("/api/approaches").status_code, 200)

    def test_real_run_end_to_end_over_http(self):
        c, svc = client(self)
        r = c.post("/api/experiments", json={**FAST, "predict": True, "label": "http e2e"}); self.assertEqual(r.status_code, 202)
        i = r.json()["id"]; wait_done(svc, i)
        self.assertEqual(c.get(f"/api/experiments/{i}").json()["status"], "completed")
        m = c.get(f"/api/experiments/{i}/metrics").json(); self.assertFalse(m["provenance"]["is_live"])
        self.assertGreaterEqual(c.get(f"/api/experiments/{i}/handoffs").json()["count"], 1)
        self.assertGreater(c.get(f"/api/experiments/{i}/events", params={"limit": 5}).json()["total"], 0)
        self.assertEqual(c.get(f"/api/experiments/{i}/predictions").json()["model"]["id"], "stage6")
        self.assertEqual(json.loads(c.get(f"/api/experiments/{i}/export", params={"format": "json"}).content)["experiment"]["id"], i)
        self.assertIn("handoff_latency_ms", c.get(f"/api/experiments/{i}/export", params={"format": "csv"}).text.splitlines()[0])
        z = c.get(f"/api/experiments/{i}/export", params={"format": "zip"}); self.assertEqual(z.content[:2], b"PK")
        self.assertIn("Stage 6", json.dumps(c.get("/api/models/stage6/evaluation").json()) + "Stage 6")


if __name__ == "__main__":
    unittest.main()
