#!/usr/bin/env python3
"""Run one real experiment through the Stage 8 service layer (SQLite + worker process), without HTTP. Works without FastAPI.
    make api-smoke        # or: PYTHONPATH=backend python scripts/api_smoke.py"""
import json
import sys
import time

from app.services.experiments import create_service
from app.services.settings import ServiceSettings


def main() -> int:
    svc = create_service(ServiceSettings.from_env())
    svc.start()
    try:
        v, _ = svc.create({"approach": "mip-basic", "scenario": "baseline", "label": "api-smoke", "seed": int(time.time()) % 100000})
        print("accepted", v["id"], v["status"])
        while svc.get(v["id"])["status"] in ("queued", "running"):
            time.sleep(0.2)
        g = svc.get(v["id"])
        print("final status:", g["status"], g["error"] or "")
        if g["status"] == "completed":
            print(json.dumps(g["result"]["summary"], indent=2))
        return 0 if g["status"] == "completed" else 1
    finally:
        svc.stop()


if __name__ == "__main__":
    sys.exit(main())
