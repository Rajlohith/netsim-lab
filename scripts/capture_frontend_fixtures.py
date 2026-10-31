#!/usr/bin/env python3
"""Capture REAL service responses as the dashboard's contract fixtures (frontend/src/test/fixtures/api/*.json).

Starts the service-layer HTTP adapter in-process (see service_http_adapter.py), runs four real experiments through the real worker
processes, waits for them and stores the responses unchanged (only long lists are paged: events/predictions limit 60). The tests then
parse exactly what the backend produced - nothing here is hand-written data.

    PYTHONPATH=backend python scripts/capture_frontend_fixtures.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services.experiments import create_service  # noqa: E402
from app.services.settings import ServiceSettings  # noqa: E402
from service_http_adapter import make_handler  # noqa: E402

OUT = ROOT / "frontend" / "src" / "test" / "fixtures" / "api"
REQUESTS = {
    "exp_basic": {"approach": "mip-basic", "scenario": "baseline", "seed": 1, "label": "fixture: mip-basic baseline"},
    "exp_mbb": {"approach": "mip-mbb", "handoff_mode": "mbb", "scenario": "lossy-access", "seed": 7, "predict": True, "label": "fixture: mbb lossy access, predictions"},
    "exp_tcp": {"approach": "tcp-reconnect", "scenario": "high-latency", "seed": 1, "label": "fixture: tcp-reconnect high latency"},
    "exp_ro": {"approach": "ro-model", "scenario": "distant-home-agent", "seed": 1, "label": "fixture: route-optimisation distant HA"},
}


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), dict(e.headers)


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def main():
    tmp = tempfile.mkdtemp(prefix="netsim-fixtures-")
    svc = create_service(ServiceSettings(data_dir=Path(tmp), allow_root=True, tick_s=0.05, cancel_grace_s=1.0).validate())
    svc.start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api"
    try:
        for name, path in (("approaches", "/approaches"), ("scenarios", "/scenarios"), ("capabilities", "/capabilities"), ("topology", "/topology"),
                           ("models", "/models"), ("model_evaluation", "/models/stage6/evaluation")):
            st, body, _ = call(base, "GET", path)
            assert st == 200, (path, st, body)
            save(name, body)
        ids = {}
        for name, req in REQUESTS.items():
            st, body, _ = call(base, "POST", "/experiments", req)
            assert st == 202, (name, st, body)
            ids[name] = body["id"]
            save(f"{name}_created", body)
        for name, eid in ids.items():
            end = time.time() + 180
            while time.time() < end:
                st, v, _ = call(base, "GET", f"/experiments/{eid}")
                if v["status"] not in ("queued", "running"):
                    break
                time.sleep(0.3)
            assert v["status"] == "completed", (name, v["status"], v.get("error"))
            save(name, v)
            for sub, q in (("metrics", ""), ("handoffs", ""), ("events", "?limit=60"), ("transitions", "")):
                st, body, _ = call(base, "GET", f"/experiments/{eid}/{sub}{q}")
                assert st == 200, (name, sub, st, body)
                save(f"{name}_{sub}", body)
            if v["prediction"]["status"] == "available":
                st, body, _ = call(base, "GET", f"/experiments/{eid}/predictions?limit=60")
                assert st == 200, body
                save(f"{name}_predictions", body)
        st, body, _ = call(base, "GET", "/experiments?limit=50")
        save("experiments_list", body)
        st, body, _ = call(base, "POST", "/experiments", {"approach": "mip-basic", "environment": "live"})
        save("error_422_live_environment", {"status": st, **body})
        st, body, _ = call(base, "GET", "/experiments/exp-0000000000000000")
        save("error_404_unknown_experiment", {"status": st, **body})
        st, body, _ = call(base, "POST", "/experiments/validate", {"approach": "mip-basic", "duration_s": 500})
        save("validate_invalid", body)
        st, body, _ = call(base, "POST", "/experiments/validate", {"approach": "mip-basic"})
        save("validate_valid", body)
        save("_ids", ids)
        print("fixtures written to", OUT.relative_to(ROOT), "-", len(list(OUT.glob("*.json"))), "files; experiments:", ids)
    finally:
        srv.shutdown()
        svc.stop()


if __name__ == "__main__":
    main()
