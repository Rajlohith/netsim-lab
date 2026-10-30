import sqlite3
import threading
import unittest

from app.services import errors as E
from app.services.catalog import Catalog
from app.services.model_registry import ModelRegistry
from app.services.settings import ServiceSettings
from app.services.store import Store
from app.services.validation import validate_request
from tests.api_helpers import FAST, tmpdir

S = ServiceSettings().validate()
C, M = Catalog(S), ModelRegistry(S)


def res(**kw):
    iss, r = validate_request({**FAST, **kw}, S, C, M)
    assert not iss, iss
    return r


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.st = Store(tmpdir(self) / "db.sqlite3")
        self.st.init()

    def ins(self, n, key=None, q=20, **kw):
        return self.st.insert(exp_id=f"exp-{n:016x}", resolved=res(**kw), idempotency_key=key, max_queue=q)

    def test_insert_persists_and_logs_transition(self):
        row, created = self.ins(1, label="hello")
        self.assertTrue(created); self.assertEqual((row["status"], row["label"], row["prediction_status"]), ("queued", "hello", "not_requested"))
        self.assertEqual(row["request"]["approach"], "mip-basic")
        again = Store(self.st.path).get("exp-0000000000000001")                    # a fresh object = a restart
        self.assertEqual(again["spec"]["scenario_id"], "api-mip-basic-emulated-baseline")
        self.assertEqual([t["to_status"] for t in self.st.transitions(row["id"])], ["queued"])

    def test_duplicate_active_configuration_is_refused(self):
        a, _ = self.ins(1)
        with self.assertRaises(E.DuplicateActiveExperiment) as cm:
            self.ins(2, label="other label, same configuration")
        self.assertEqual(cm.exception.details["existing_experiment_id"], a["id"])
        self.ins(3, seed=1)                                                          # a different configuration is fine

    def test_duplicate_allowed_again_once_the_first_is_terminal(self):
        a, _ = self.ins(1)
        self.assertTrue(self.st.transition(a["id"], "cancelled", expect=["queued"]))
        row, created = self.ins(2)
        self.assertTrue(created)

    def test_unique_index_is_the_real_guard(self):
        self.ins(1)
        c = sqlite3.connect(self.st.path)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO experiments (id,created_at,updated_at,status,approach,scenario,environment,request_json,spec_json,fingerprint,request_hash) "
                      "SELECT 'exp-ffffffffffffffff',created_at,updated_at,'queued',approach,scenario,environment,request_json,spec_json,fingerprint,request_hash FROM experiments")
        c.close()

    def test_concurrent_identical_submissions_create_exactly_one(self):
        out, errs = [], []

        def go(i):
            try:
                out.append(self.ins(100 + i)[0]["id"])
            except E.DuplicateActiveExperiment as e:
                errs.append(e)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual((len(out), len(errs)), (1, 11))

    def test_idempotency_key(self):
        a, c1 = self.ins(1, key="k1")
        b, c2 = self.ins(2, key="k1")
        self.assertEqual((c1, c2, a["id"], b["id"]), (True, False, a["id"], a["id"]))
        with self.assertRaises(E.IdempotencyKeyReused):
            self.ins(3, key="k1", seed=9)
        self.st.transition(a["id"], "cancelled", expect=["queued"])
        self.assertFalse(self.ins(4, key="k1")[1])                                    # replay still returns the original, even when finished

    def test_queue_limit(self):
        self.ins(1, q=2, seed=1); self.ins(2, q=2, seed=2)
        with self.assertRaises(E.QueueFull) as cm:
            self.ins(3, q=2, seed=3)
        self.assertEqual(cm.exception.headers["Retry-After"], "30")

    def test_state_machine_only_allows_documented_moves(self):
        a, _ = self.ins(1)
        self.assertFalse(self.st.transition(a["id"], "completed"))                   # queued -> completed is not allowed
        self.assertFalse(self.st.transition(a["id"], "running", expect=["running"]))
        self.assertTrue(self.st.transition(a["id"], "running", expect=["queued"]))
        self.assertTrue(self.st.transition(a["id"], "completed", expect=["running"], run_status="ok"))
        for to in ("running", "failed", "cancelled", "incomplete", "queued"):
            self.assertFalse(self.st.transition(a["id"], to), to)                     # terminal states are final
        row = self.st.get(a["id"])
        self.assertIsNotNone(row["finished_at"]); self.assertEqual(row["run_status"], "ok")
        self.assertEqual([t["to_status"] for t in self.st.transitions(a["id"])], ["queued", "running", "completed"])
        with self.assertRaises(ValueError):
            self.st.transition(a["id"], "failed", status="x")

    def test_claim_is_atomic_fifo(self):
        for i in range(3):
            self.ins(i + 1, seed=i)
        ids = []

        def claim():
            r = self.st.claim_next("o")
            if r:
                ids.append(r["id"])
        ts = [threading.Thread(target=claim) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(ids), [f"exp-{i:016x}" for i in (1, 2, 3)]); self.assertEqual(len(set(ids)), 3)

    def test_list_filters_sort_pagination_delete(self):
        for i in range(5):
            self.ins(i + 1, seed=i, label=f"run {i}", **({"approach": "mip-mbb"} if i % 2 else {}))
        self.st.transition("exp-0000000000000001", "cancelled", expect=["queued"])
        rows, total = self.st.list(limit=2, offset=1)
        self.assertEqual((total, len(rows)), (5, 2))
        self.assertEqual(self.st.list(statuses=["cancelled"])[1], 1)
        self.assertEqual(self.st.list(approach="mip-mbb")[1], 2)
        self.assertEqual(self.st.list(label_contains="RUN 3")[1], 1)
        self.assertEqual(self.st.list(sort="created_at", descending=False)[0][0]["id"], "exp-0000000000000001")
        with self.assertRaises(ValueError):
            self.st.list(sort="id; DROP TABLE experiments")
        self.assertFalse(self.st.delete("exp-0000000000000002"))                      # active: refused
        self.assertTrue(self.st.delete("exp-0000000000000001"))
        self.assertEqual(self.st.transitions("exp-0000000000000001"), [])             # log removed with it (cascade)

    def test_schema_version_mismatch_is_refused(self):
        c = sqlite3.connect(self.st.path); c.execute("UPDATE meta SET value='99'"); c.commit(); c.close()
        with self.assertRaises(RuntimeError):
            Store(self.st.path).init()


if __name__ == "__main__":
    unittest.main()
