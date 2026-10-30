#!/usr/bin/env python3
"""Write the OpenAPI document of the running application code to schemas/api/openapi.json (needs fastapi installed; no server is started).

    python scripts/export_openapi.py [--check]

--check exits 1 if schemas/api/openapi.json exists and differs from what the code produces (use it in CI)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "schemas" / "api" / "openapi.json"


def main() -> int:
    try:
        from app.main import create_app
    except ImportError as e:
        print(f"cannot import the application ({e}); install requirements.txt first", file=sys.stderr)
        return 2
    doc = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != doc:
            print(f"{OUT} is missing or out of date: run python scripts/export_openapi.py", file=sys.stderr)
            return 1
        print("openapi.json is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc)
    print(f"wrote {OUT} ({len(doc)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
