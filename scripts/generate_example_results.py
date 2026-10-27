#!/usr/bin/env python3
"""Regenerate examples/results/ with the REAL sender/receiver/analysis code over loopback.

The impairments come from the userspace proxy (app/traffic/loopback_proxy.py), not from tc netem,
and no namespaces/root are involved. Run:  python scripts/generate_example_results.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.traffic.analyze import analyze_run, summary_text            # noqa: E402
from app.traffic.loopback_proxy import PathRules                      # noqa: E402
from tests.traffic_helpers import run_loopback                        # noqa: E402

OUT = ROOT / "examples" / "results"


def killed_sender(out: Path, scenario: str, run: str):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "backend"))
    ready = out / "ready"
    mod = lambda m, *a: subprocess.Popen([sys.executable, "-m", f"app.traffic.{m}", *map(str, a)], env=env,
                                         cwd=ROOT / "backend", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rx = mod("receiver", "--scenario-id", scenario, "--run-id", run, "--out-dir", out, "--bind", "127.0.0.1",
             "--port", 0, "--ready-file", ready, "--idle-timeout", 1.0)
    while not (ready.exists() and ready.read_text().strip()):
        time.sleep(0.02)
    tx = mod("sender", f"127.0.0.1:{ready.read_text().strip()}", "--scenario-id", scenario, "--run-id", run,
             "--out-dir", out, "--rate", 200, "--duration", 60)
    time.sleep(1.0)
    tx.send_signal(signal.SIGKILL)                  # no chance to finalise
    tx.wait()
    rx.wait()
    ready.unlink()
    return analyze_run(out / scenario / run)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    runs = {
        "example-loss-by-seq": run_loopback(OUT, PathRules(drop_seqs={5, 17, 18, 19, 40}), count=100, rate=200,
                                            scenario="example-loss-by-seq", run="example-1"),
        "example-delay-20ms": run_loopback(OUT, PathRules(delay_ms=20), count=100, rate=100,
                                           scenario="example-delay-20ms", run="example-1"),
        "example-handoff-blackout": run_loopback(
            OUT, PathRules(delay_schedule=[(0, 10), (1.4, 30)], blackouts=[(1.0, 1.4)]), duration=3.0, rate=100,
            scenario="example-handoff-blackout", run="example-1"),
    }
    for name, (m, *_rest) in runs.items():
        print(summary_text(m), "\n")
    print(summary_text(killed_sender(OUT, "example-sigkill-sender", "example-1")))


if __name__ == "__main__":
    main()
