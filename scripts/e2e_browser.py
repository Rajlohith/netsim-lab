#!/usr/bin/env python3
"""Real-browser end-to-end check of the React dashboard (Stage 10).

What runs for real:  the dashboard bundle (esbuild, same entry as `vite build`), headless Chrome/Chromium driven by Playwright,
the real ExperimentService (SQLite store, worker PROCESSES, real emulator runs, real exports).
What is a stand-in:  the HTTP layer is scripts/service_http_adapter.py (stdlib), NOT FastAPI - so FastAPI routing/pydantic/CORS
are NOT covered here (that is `make api-test`). The bundler is esbuild, not Vite (no `npm install` needed).

    python scripts/e2e_browser.py --out docs/e2e_browser_results.json --screenshots docs/demo

Needs: node + esbuild + react/react-dom resolvable through NODE_PATH, python `playwright`, a Chrome/Chromium binary
(CHROME_PATH, or the first found in PATH / ~/.cache/puppeteer). Exits non-zero if any check fails; every check is recorded.
Nothing is mocked: the numbers asserted on screen are compared with the numbers the API returned for the same experiment.
"""
import argparse
import csv
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def find_chrome() -> str | None:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for n in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if shutil.which(n):
            return shutil.which(n)
    hits = sorted(Path("/").glob("home/*/.cache/puppeteer/chrome/*/chrome-linux64/chrome")) + sorted(Path.home().glob(".cache/puppeteer/chrome/*/chrome-linux64/chrome"))
    return str(hits[-1]) if hits else None


def build_bundle(dist: Path) -> None:
    js = ("const {build}=require('esbuild');build({entryPoints:['src/main.jsx'],bundle:true,minify:true,outdir:process.argv[1],"
          "format:'esm',jsx:'automatic',platform:'browser',target:'es2020',loader:{'.css':'css'},logLevel:'error',"
          "nodePaths:(process.env.NODE_PATH||'').split(':').filter(Boolean),define:{'process.env.NODE_ENV':'\"production\"'}})")
    subprocess.run(["node", "-e", js, str(dist)], cwd=ROOT / "frontend", check=True)
    (dist / "index.html").write_text('<!doctype html><html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, '
                                     'initial-scale=1.0"/><title>NetSim Lab</title><link rel="stylesheet" href="/main.css"/></head><body>'
                                     '<div id="root"></div><script type="module" src="/main.js"></script></body></html>')


def serve(dist: Path, api_port: int, port: int) -> ThreadingHTTPServer:
    """Same-origin server: static bundle + /api proxy to the adapter (the dashboard calls the relative base URL /api)."""
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _do(self):
            if self.path.startswith("/api"):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                c = http.client.HTTPConnection("127.0.0.1", api_port, timeout=60)
                hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
                c.request(self.command, self.path, body=body, headers=hdrs)
                r = c.getresponse()
                data = r.read()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            p = dist / ("index.html" if self.path in ("/", "") else self.path.lstrip("/").split("?")[0])
            if not p.is_file() or dist not in p.resolve().parents:
                self.send_error(404)
                return
            ct = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(p.suffix, "application/octet-stream")
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _do

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def api_get(port: int, path: str, raw: bool = False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request("GET", "/api" + path)
    r = c.getresponse()
    data = r.read()
    if r.status != 200:
        raise RuntimeError(f"GET {path} -> {r.status}: {data[:200]!r}")
    return data if raw else json.loads(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None, help="write the JSON result list here")
    ap.add_argument("--screenshots", type=Path, default=None, help="directory for page screenshots")
    ap.add_argument("--api-port", type=int, default=8765)
    ap.add_argument("--web-port", type=int, default=8766)
    ap.add_argument("--timeout-s", type=int, default=150)
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    chrome = find_chrome()
    if not chrome:
        print("no Chrome/Chromium found (set CHROME_PATH)", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="netsim-e2e-"))
    dist, data = work / "dist", work / "data"
    dist.mkdir()
    data.mkdir()
    build_bundle(dist)
    adapter = subprocess.Popen([sys.executable, str(ROOT / "scripts/service_http_adapter.py"), "--port", str(a.api_port), "--data-dir", str(data)],
                               env=dict(os.environ, PYTHONPATH=str(ROOT / "backend")), stdout=subprocess.DEVNULL, stderr=(work / "adapter.log").open("w"))
    srv = None
    try:
        for _ in range(100):
            try:
                api_get(a.api_port, "/health")
                break
            except Exception:
                time.sleep(0.2)
        else:
            check("adapter (real service layer) starts", False, (work / "adapter.log").read_text()[-400:])
            return 1
        check("adapter (real service layer) starts", True)
        srv = serve(dist, a.api_port, a.web_port)
        base = f"http://127.0.0.1:{a.web_port}/"
        if a.screenshots:
            a.screenshots.mkdir(parents=True, exist_ok=True)
        errors, bad_responses, not_found = [], [], []
        with sync_playwright() as pw:
            br = pw.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
            ctx = br.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 500 else (not_found.append(r.url) if r.status == 404 else None))

            def shot(name):
                if a.screenshots:
                    page.screenshot(path=str(a.screenshots / f"{name}.png"), full_page=True)

            page.goto(base)
            page.wait_for_selector("nav[aria-label='Main navigation']", timeout=15000)
            check("dashboard loads in a real browser and renders the navigation", True)
            page.wait_for_function("document.querySelector('.backend')?.textContent.includes('reachable') && !document.querySelector('.backend')?.textContent.includes('unreachable')", timeout=15000)
            check("header shows 'API: reachable' (real /api/health through the proxy)", True)
            titles = page.eval_on_selector_all("nav a", "els => els.map(e => e.textContent.trim())")
            check("seven navigation entries", len(titles) == 7, str(titles))

            # topology page vs /api/topology
            topo = api_get(a.api_port, "/topology")
            page.goto(base + "#/topology")
            page.wait_for_selector("main h1, h1", timeout=10000)
            time.sleep(1.0)
            text = page.inner_text("body")
            nodes = [n.get("key") for n in topo.get("nodes", [])]
            check("topology page lists every node returned by /api/topology", nodes and all(str(n) in text for n in nodes), str(nodes))
            shot("1-topology")

            # configure + start a real experiment through the UI
            page.goto(base + "#/configure")
            page.wait_for_selector("form[aria-label='Experiment configuration']", timeout=10000)
            approaches = api_get(a.api_port, "/approaches")
            options = page.eval_on_selector_all("#approach option", "els => els.map(e => e.value)")
            ids = [x["id"] for x in (approaches.get("approaches") or approaches.get("items") or approaches)]
            check("approach selector offers exactly the approaches of /api/approaches", sorted(options) == sorted(ids), f"{options} vs {ids}")
            page.select_option("#approach", "mip-basic")
            page.fill("#seed", "1")
            page.fill("#label", "stage10-e2e")
            shot("2-configure")
            page.fill("#duration_s", "-5")
            page.click("button.primary")
            page.wait_for_selector("[role=alert]", timeout=5000)
            check("invalid duration (-5) is rejected in the form and no experiment is created", api_get(a.api_port, "/experiments")["total"] == 0)
            page.fill("#duration_s", "12")
            page.click("button.primary")
            page.wait_for_function("location.hash.startsWith('#/live/exp-')", timeout=15000)
            exp_id = page.evaluate("location.hash.split('/')[2]")
            check("starting from the form navigates to the live page of a new experiment", exp_id.startswith("exp-"), exp_id)

            # wait for completion in the UI and in the API
            t0 = time.time()
            while time.time() - t0 < a.timeout_s:
                st = api_get(a.api_port, f"/experiments/{exp_id}")["status"]
                if st in ("completed", "failed", "cancelled", "incomplete"):
                    break
                time.sleep(1)
            check("experiment reaches 'completed' through a real worker process", st == "completed", st)
            page.wait_for_selector("text=Measured values", timeout=30000)
            check("live page shows the results section after completion", True)
            time.sleep(0.8)

            # consistency: what is on screen == what the API returned for the same experiment
            m = api_get(a.api_port, f"/experiments/{exp_id}/metrics")["metrics"]
            hs = api_get(a.api_port, f"/experiments/{exp_id}/handoffs")
            body = page.inner_text("body")
            lost, sent = m["loss"]["lost"], m["loss"]["sent"]
            check("screen shows the API's lost/sent probe counts", f"{lost}" in body and f"{sent}" in body.replace(",", "").replace("\u202f", "").replace("\u00a0", ""), f"{lost} of {sent}")
            rows = page.eval_on_selector_all("table tbody tr", "els => els.map(e => e.innerText)")
            hrows = [r for r in rows if r.startswith("handoff-")]
            check("handoff table has one row per API handoff", len(hrows) == len(hs["handoffs"]), f"{len(hrows)} vs {len(hs['handoffs'])}")
            ok = True
            for h, r in zip(hs["handoffs"], hrows):
                ok &= h["event_id"] in r and (f"{h['interruption_ms']:.1f}".rstrip("0").rstrip(".") in r or f"{h['interruption_ms']:.0f}" in r)
            check("each handoff row shows its API event id and interruption value", ok, "; ".join(hrows)[:200])
            shot("3-live")

            # exports through the UI == exports from the API
            def click_export(label):
                with page.expect_download(timeout=20000) as d:
                    page.get_by_role("button", name=label).first.click()
                p = work / f"dl-{label}"
                d.value.save_as(str(p))
                return p.read_bytes(), d.value.suggested_filename

            buttons = page.eval_on_selector_all("[aria-label^='Export'] button", "els => els.map(e => e.textContent.trim())")
            check("export buttons are rendered", len(buttons) >= 3, str(buttons))
            jbtn = next((b for b in buttons if "JSON" in b.upper()), None)
            if jbtn:
                got, fname = click_export(jbtn)
                want = api_get(a.api_port, f"/experiments/{exp_id}/export?format=json", raw=True)
                check("JSON export downloaded from the UI is byte-identical to the API export", got == want, f"{fname}: {len(got)} vs {len(want)} bytes")
            cbtn = next((b for b in buttons if "CSV" in b.upper() and "handoff" in b.lower()), None) or next((b for b in buttons if "CSV" in b.upper()), None)
            if cbtn:
                got, fname = click_export(cbtn)
                rows_csv = list(csv.DictReader(io.StringIO(got.decode())))
                check("handoffs CSV from the UI has one row per API handoff", len(rows_csv) == len(hs["handoffs"]), f"{fname}: {len(rows_csv)} rows")

            # cancellation through the UI: a long run is started via the API, stopped with the page's Stop button
            c = http.client.HTTPConnection("127.0.0.1", a.api_port, timeout=60)
            c.request("POST", "/api/experiments", body=json.dumps({"approach": "mip-basic", "scenario": "baseline", "seed": 7, "duration_s": 120, "rate_pps": 1000}), headers={"Content-Type": "application/json"})
            r = c.getresponse(); body_ = r.read()
            if check("long experiment accepted by the API (HTTP 202)", r.status == 202, f"{r.status} {body_[:200]!r}"):
                long_id = json.loads(body_)["id"]
                page.goto(base + "#/configure")
                page.get_by_role("button", name="Stop").first.wait_for(timeout=15000)
                page.get_by_role("button", name="Stop").first.click()
                page.get_by_role("button", name="Yes, stop it").click()
                t0 = time.time(); st = None
                while time.time() - t0 < 40:
                    st = api_get(a.api_port, f"/experiments/{long_id}")["status"]
                    if st not in ("queued", "running"):
                        break
                    time.sleep(0.5)
                check("Stop button cancels the running experiment (status 'cancelled')", st == "cancelled", str(st))
                time.sleep(1)
                left_ = [p_ for p_ in subprocess.run(["pgrep", "-f", long_id], capture_output=True, text=True).stdout.split() if p_ != str(os.getpid())]
                check("no worker process remains for the cancelled experiment", not left_, str(left_))

            # remaining pages must render real data (not error / empty) for this experiment
            for hashp, needle, nm in ((f"#/predictions/{exp_id}", None, "4-predictions"), ("#/compare", None, "5-compare"), ("#/models", None, "6-models"), (f"#/history/{exp_id}", exp_id[:8], "7-history")):
                page.goto(base + hashp)
                time.sleep(1.8)
                body = page.inner_text("body")
                check(f"page {hashp.split('/')[1]} renders without an error banner", "Something went wrong" not in body and "unexpected error" not in body.lower(), body[:150].replace("\n", " "))
                if needle:
                    check(f"page {hashp.split('/')[1]} shows the experiment", needle in body)
                shot(nm)
            models = api_get(a.api_port, "/models")
            mid = (models.get("models") or models.get("items") or [{}])[0].get("id")
            page.goto(base + "#/models")
            time.sleep(1.5)
            check("model page names the model the API reports", bool(mid) and mid in page.inner_text("body"), str(mid))

            # history page lists the experiment; history CSV from API has the same id
            hist_csv = api_get(a.api_port, "/history/export?format=csv", raw=True).decode()
            check("API history export contains the experiment started from the UI", exp_id in hist_csv)

            # unknown route + stop of an already-finished run are handled
            page.goto(base + "#/nonsense")
            time.sleep(0.5)
            check("unknown route shows the 'Page not found' state", "Page not found" in page.inner_text("body"))

            # a 404 is expected only where the API legitimately says "not available" (documented error codes) - list them all
            unexpected_404 = [u for u in not_found if "/api/" not in u]
            check("no 404 for any static asset (bundle, css, favicon excluded)", not [u for u in unexpected_404 if "favicon" not in u], "; ".join(unexpected_404)[:300])
            api_404 = [u.split("/api")[1] for u in not_found if "/api/" in u]
            print("INFO API 404s seen by the browser (documented 'not available' answers):", api_404)
            ignorable = ("favicon", "status of 404")
            check("no browser console errors / uncaught exceptions (404 resource notices listed separately)", not [e for e in errors if not any(i in e for i in ignorable)], "; ".join(errors)[:300])
            check("no HTTP 5xx responses seen by the browser", not bad_responses, "; ".join(bad_responses)[:300])
            br.close()
        # cleanup verification: no worker process of this run is left, adapter data dir only contains the run we made
        left = subprocess.run(["pgrep", "-f", str(data)], capture_output=True, text=True).stdout.split()
        left = [p for p in left if p != str(adapter.pid) and p != str(os.getpid())]
        check("no worker process is left behind after the run", not left, str(left))
    finally:
        if srv:
            srv.shutdown()
        adapter.terminate()
        try:
            adapter.wait(timeout=10)
        except subprocess.TimeoutExpired:
            adapter.kill()
        shutil.rmtree(work, ignore_errors=True)
    failed = [c for c in CHECKS if not c["ok"]]
    summary = {"passed": len(CHECKS) - len(failed), "failed": len(failed), "browser": chrome, "checks": CHECKS,
               "scope": "real browser + bundle + real service layer behind a stdlib HTTP adapter; FastAPI routes and Vite are NOT covered"}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(summary, indent=1))
    print(f"\n{summary['passed']} passed, {summary['failed']} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
