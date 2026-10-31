#!/usr/bin/env python3
"""VERIFICATION HARNESS ONLY - not part of the product and NOT the FastAPI application.

FastAPI/uvicorn/httpx are not installed in some environments (this was the case when Stage 9 was built), so the real HTTP layer
(`backend/app/main.py`, `backend/app/api/`) cannot be started there. This script serves the *same* `ExperimentService` (real
worker processes, real simulator runs, real SQLite store, real exports) over a standard-library HTTP server using the documented URL
scheme, so the dashboard's API client can be exercised against genuine service output.

What it does NOT verify: FastAPI routing, pydantic request parsing, CORS, bearer-token dependency, Swagger. Run `make api-test` and
`make run-api` on a machine with the dependencies for those.

    PYTHONPATH=backend python scripts/service_http_adapter.py --port 8765 --data-dir /tmp/netsim-adapter
"""
import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.errors import ServiceError  # noqa: E402
from app.services.experiments import create_service  # noqa: E402
from app.services.settings import ServiceSettings  # noqa: E402


def envelope(code, message, issues=None, context=None):
    return {"error": {"code": code, "message": message, "request_id": None, "issues": issues or [], "context": context or {}}}


def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, status, body=None, headers=None, raw=None, ctype="application/json"):
            data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Location, Idempotent-Replayed")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _download(self, out):
            if out["path"] is not None:
                raw = Path(out["path"]).read_bytes()
                if out.get("cleanup"):
                    Path(out["path"]).unlink(missing_ok=True)
            else:
                raw = out["content"] if isinstance(out["content"], bytes) else out["content"].encode()
            self._send(200, raw=raw, ctype=out["media_type"], headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                raise ServiceError("the request body is not valid JSON", code="invalid_json") from None

        def do_OPTIONS(self):
            self._send(204, headers={"Access-Control-Allow-Methods": "GET, POST, DELETE", "Access-Control-Allow-Headers": "*"})

        def _route(self, method):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            one = lambda k, d=None: q[k][0] if k in q else d
            p = u.path
            if p == "/api/health":
                return self._send(200, {"status": "ok"})
            m = re.fullmatch(r"/api/experiments/(exp-[0-9a-f]{16})(?:/([a-z]+))?", p)
            if method == "GET":
                simple = {"/api/approaches": svc.approaches, "/api/scenarios": svc.scenarios, "/api/capabilities": svc.capabilities,
                          "/api/status": svc.status, "/api/topology": svc.topology, "/api/models": svc.models_list}
                if p in simple:
                    return self._send(200, simple[p]())
                mm = re.fullmatch(r"/api/models/([\w.-]+)/evaluation", p)
                if mm:
                    return self._send(200, svc.model_evaluation(mm.group(1)))
                if p == "/api/experiments":
                    return self._send(200, svc.list(statuses=q.get("status"), approach=one("approach"), scenario=one("scenario"), environment=one("environment"),
                                                    created_after=one("created_after"), created_before=one("created_before"), label_contains=one("label"),
                                                    sort=one("sort", "-created_at"), limit=int(one("limit", 50)), offset=int(one("offset", 0))))
                if p == "/api/history/export":
                    return self._download(svc.export_history(one("format", "csv"), statuses=q.get("status"), approach=one("approach"), scenario=one("scenario"),
                                                             environment=one("environment"), created_after=one("created_after"), created_before=one("created_before")))
                if m:
                    eid, sub = m.group(1), m.group(2)
                    if sub is None:
                        return self._send(200, svc.get(eid))
                    if sub == "transitions":
                        return self._send(200, svc.transitions(eid, int(one("after_seq", 0)), int(one("limit", 200))))
                    if sub == "metrics":
                        return self._send(200, svc.metrics(eid))
                    if sub == "handoffs":
                        return self._send(200, svc.handoffs(eid))
                    if sub == "events":
                        return self._send(200, svc.events(eid, int(one("after_seq", 0)), int(one("limit", 500))))
                    if sub == "predictions":
                        return self._send(200, svc.predictions(eid, int(one("limit", 500)), int(one("offset", 0))))
                    if sub == "export":
                        return self._download(svc.export(eid, one("format", "json"), one("table", "handoffs"), one("include_partial", "false") == "true"))
            if method == "POST":
                if p == "/api/experiments/validate":
                    return self._send(200, svc.validate(self._body()))
                if p == "/api/experiments":
                    view, created = svc.create(self._body(), self.headers.get("Idempotency-Key"))
                    return self._send(202 if created else 200, view, headers={"Location": view["links"]["self"], **({} if created else {"Idempotent-Replayed": "true"})})
                if m and m.group(2) == "cancel":
                    view, done = svc.cancel(m.group(1))
                    return self._send(200 if done else 202, view)
            return self._send(404, envelope("not_found", "no such route"))

        def _handle(self, method):
            try:
                self._route(method)
            except ServiceError as e:
                issues = e.details if isinstance(e.details, list) else []
                ctx = e.details if isinstance(e.details, dict) else {}
                self._send(e.http_status, envelope(e.code, e.message, issues, ctx), headers=e.headers)
            except Exception as e:  # noqa: BLE001 - harness: report, never crash the server
                self._send(500, envelope("internal_error", f"{type(e).__name__}: {e}"))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", required=True)
    a = ap.parse_args()
    svc = create_service(ServiceSettings(data_dir=Path(a.data_dir), allow_root=True, tick_s=0.05, cancel_grace_s=1.0).validate())
    svc.start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(svc))
    print(f"service-layer HTTP adapter (verification harness) on http://127.0.0.1:{a.port}/api", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
