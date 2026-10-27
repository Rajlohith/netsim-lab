#!/usr/bin/env python3
"""Run every test module in isolation and write docs/test_results.json + docs/TEST_REPORT.md from the ACTUAL outcome.

    python scripts/run_all_tests.py            # from the repository root; no pytest needed (unittest only)

Modules that cannot even be imported because a third-party package is missing (fastapi, pydantic, pytest, httpx) are reported
as NOT RUNNABLE, never as passed. Function-style (pytest-style) test files are executed by a tiny built-in runner."""
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
RESULT = re.compile(r"\.\.\. (ok|FAIL|ERROR|skipped '(.*)'|expected failure|unexpected success)\s*$")


def run_module(mod: str) -> dict:
    t0 = time.time()
    p = subprocess.run([sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", f"tests.{mod}", "-v"], cwd=BACKEND,
                       capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(BACKEND)), timeout=600)
    out = p.stdout + p.stderr
    r = {"module": mod, "seconds": round(time.time() - t0, 1), "ran": 0, "failures": 0, "errors": 0, "skipped": 0, "skip_reasons": [],
         "status": "unknown", "detail": ""}
    missing = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", out)
    m = re.search(r"^Ran (\d+) tests? in", out, re.M)
    if missing and (not m or int(m.group(1)) <= 1):
        r.update(status="not-runnable", detail=f"missing package: {missing.group(1)}")
        return r
    if not m:
        r.update(status="error", detail=out[-800:])
        return r
    r["ran"] = int(m.group(1))
    s = re.search(r"^(OK|FAILED)(?: \((.*)\))?$", out, re.M)
    if s and s.group(2):
        for k, v in re.findall(r"(failures|errors|skipped)=(\d+)", s.group(2)):
            r[k] = int(v)
    r["skip_reasons"] = sorted({x.group(2) for line in out.splitlines() if (x := RESULT.search(line)) and x.group(2) is not None})
    r["status"] = "pass" if (s and s.group(1) == "OK") else "FAIL"
    if r["status"] == "FAIL":
        r["detail"] = out[-1500:]
    if r["ran"] == 0:
        r["status"] = "no-unittest-tests"
    return r


def run_function_style(mod: str) -> dict:
    code = (f"import sys; sys.path.insert(0,'.'); import importlib, traceback\n"
            f"m = importlib.import_module('tests.{mod}'); ok = bad = 0; fails = []\n"
            "for n in sorted(dir(m)):\n"
            "    if n.startswith('test_') and callable(getattr(m, n)):\n"
            "        try: getattr(m, n)(); ok += 1\n"
            "        except Exception: bad += 1; fails.append(n + ': ' + traceback.format_exc()[-300:])\n"
            "print(ok, bad); print('\\n'.join(fails))\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(BACKEND)))
    first = p.stdout.splitlines()[0].split() if p.stdout.strip() else ["0", "1"]
    ok, bad = int(first[0]), int(first[1])
    return {"module": mod, "seconds": 0.0, "ran": ok + bad, "failures": bad, "errors": 0, "skipped": 0, "skip_reasons": [],
            "status": "pass" if bad == 0 and ok else "FAIL", "detail": p.stdout[-800:] + p.stderr[-800:], "style": "function (pytest-style, run by the built-in runner)"}


def environment() -> dict:
    import importlib.util as u
    raw = netns = None
    try:
        import socket
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW).close()
        raw = True
    except OSError:
        raw = False
    netns = subprocess.run(["unshare", "-Urn", "true"], capture_output=True).returncode == 0 if shutil.which("unshare") else False
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None
    dirty = [l for l in subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
             if "TEST_REPORT.md" not in l and "test_results.json" not in l]
    git = (git + (" (+uncommitted changes)" if dirty else "")) if git else None
    return {"python": platform.python_version(), "platform": platform.platform(), "uid": os.geteuid(), "git_commit": git,
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            "tools": {t: bool(shutil.which(t)) for t in ("ip", "tc", "ping", "sudo", "setpriv", "nsenter", "unshare", "node", "npm")},
            "packages": {m: bool(u.find_spec(m)) for m in ("aioquic", "cryptography", "fastapi", "uvicorn", "pydantic", "pydantic_settings", "httpx", "pytest", "yaml", "numpy", "pandas", "sklearn", "matplotlib")},
            "raw_sockets": raw, "unprivileged_netns": netns}


def main():
    names = sorted(Path(p).stem for p in glob.glob(str(BACKEND / "tests" / "test_*.py")))
    results = []
    for n in names:
        r = run_module(n)
        if r["status"] == "no-unittest-tests":
            r = run_function_style(n)
        print(f"{n:34} {r['status']:13} ran={r['ran']:3} fail={r['failures']} err={r['errors']} skip={r['skipped']}  {r['seconds']}s", flush=True)
        results.append(r)
    tot = {k: sum(r[k] for r in results) for k in ("ran", "failures", "errors", "skipped")}
    tot["passed"] = tot["ran"] - tot["failures"] - tot["errors"] - tot["skipped"]
    tot["modules"] = len(results)
    tot["not_runnable_modules"] = [r["module"] for r in results if r["status"] == "not-runnable"]
    tot["failed_modules"] = [r["module"] for r in results if r["status"] in ("FAIL", "error")]
    data = {"environment": environment(), "totals": tot, "modules": results}
    (ROOT / "docs" / "test_results.json").write_text(json.dumps(data, indent=2, sort_keys=True))
    L = ["# Test report (generated by scripts/run_all_tests.py — do not edit by hand)", "",
         f"Generated {data['environment']['generated_utc']} on {data['environment']['platform']} (Python {data['environment']['python']}, "
         f"git {data['environment']['git_commit']}).", "",
         f"**Totals: {tot['passed']} passed, {tot['failures'] + tot['errors']} failed, {tot['skipped']} skipped, "
         f"{len(tot['not_runnable_modules'])} module(s) not runnable** ({tot['ran']} tests in {tot['modules']} modules).", "",
         "| Module | Status | Ran | Failed | Skipped | Seconds |", "|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| `{r['module']}` | {r['status']}{' — ' + r['detail'] if r['status'] == 'not-runnable' else ''} | {r['ran']} | "
                 f"{r['failures'] + r['errors']} | {r['skipped']} | {r['seconds']} |")
    L += ["", "## Skipped tests and why", ""]
    for r in results:
        for s in r["skip_reasons"]:
            L.append(f"* `{r['module']}`: {s}")
    e = data["environment"]
    L += ["", "## Environment capabilities that decide what could run", "",
          "Tools: " + ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in e["tools"].items()),
          "", "Packages: " + ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in e["packages"].items()),
          "", f"Raw sockets: {e['raw_sockets']}; unprivileged network namespaces: {e['unprivileged_netns']}; running as uid {e['uid']}"]
    (ROOT / "docs" / "TEST_REPORT.md").write_text("\n".join(L) + "\n")
    print("\nTOTAL:", tot)
    return 1 if tot["failed_modules"] else 0


if __name__ == "__main__":
    sys.exit(main())
