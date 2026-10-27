import json
import threading
import time
from pathlib import Path

from app.traffic import clock
from app.traffic.analyze import analyze_run
from app.traffic.loopback_proxy import ImpairmentProxy, PathRules
from app.traffic.receiver import ProbeReceiver, ReceiverConfig
from app.traffic.recorder import EventLog
from app.traffic.sender import ProbeSender, SenderConfig

BACKEND = Path(__file__).resolve().parents[1]


def run_loopback(tmp, rules: PathRules, *, rate=200.0, count=None, duration=None, scenario="lb", run="r1",
                 drain=0.5, size=64, margin_ms=250.0):
    """sender -> impairment proxy -> receiver(echo) on loopback; returns (metrics, run_dir, proxy_stats)."""
    run_dir = Path(tmp) / scenario / run
    run_dir.mkdir(parents=True)
    (run_dir / "scenario.json").write_text(json.dumps(
        {"scenario_id": scenario, "run_id": run, "clock": clock.clock_info()}))
    events = EventLog(run_dir) if rules.blackouts else None
    rx = ProbeReceiver(ReceiverConfig(scenario, tmp, run, bind_addr="127.0.0.1", port=0, idle_timeout_s=0.7))
    errs = []
    th = threading.Thread(target=lambda: _guard(rx.run, errs))
    th.start()
    for _ in range(500):
        if rx.port:
            break
        time.sleep(0.01)
    proxy = ImpairmentProxy(("127.0.0.1", rx.port), rules, event_log=events).start()
    tx = ProbeSender(SenderConfig(proxy.front_addr, scenario, tmp, run, rate_pps=rate, count=count,
                                  duration_s=duration, size=size, drain_s=drain))
    tx.run()
    th.join(timeout=15)
    proxy.stop()
    if events:
        events.close()
    assert not errs, errs
    return analyze_run(run_dir, handoff_margin_ms=margin_ms), run_dir, proxy.stats


def _guard(fn, errs):
    try:
        fn()
    except BaseException as e:      # surfaced by the caller's assertion
        errs.append(e)
