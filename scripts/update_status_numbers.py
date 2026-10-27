#!/usr/bin/env python3
"""Refresh the generated blocks of PROJECT_STATUS.md (stamp, test headline, per-module table) from docs/test_results.json.
Run after scripts/run_all_tests.py. Everything outside the BEGIN/END markers is hand-written text."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = json.loads((ROOT / "docs" / "test_results.json").read_text())
T, E = R["totals"], R["environment"]
nr = ", ".join(f"`{m}`" for m in T["not_runnable_modules"])
blocks = {
    "stamp": f"**{E['generated_utc']}**, git commit `{E['git_commit']}`, Python {E['python']}, {E['platform']}.",
    "headline": (f"* **Tests: {T['ran']} ran -> {T['passed']} passed, {T['failures'] + T['errors']} failed, {T['skipped']} skipped; "
                 f"{len(T['not_runnable_modules'])} modules not runnable** ({nr}: FastAPI / pydantic / pytest are not installed in the sandbox, "
                 "so the Stage 1 API and settings tests have **never been executed**)."),
}
rows = "\n".join(f"| `{m['module']}` | {m['status']} | {m['ran']} | {m['failures'] + m['errors']} | {m['skipped']} |" for m in R["modules"])
skips = {}
for m in R["modules"]:
    for s in m["skip_reasons"]:
        skips.setdefault(s, []).append(m["module"])
skip_rows = "\n".join(f"| {', '.join('`' + x + '`' for x in v)} | {k} |" for k, v in skips.items())
blocks["tests"] = (f"Totals: **{T['passed']} passed / {T['failures'] + T['errors']} failed / {T['skipped']} skipped / {len(T['not_runnable_modules'])} not runnable** "
                   "-- per-module table below, full detail in `docs/TEST_REPORT.md` and `docs/test_results.json`.\n\n"
                   f"| Module | Status | Ran | Failed | Skipped |\n|---|---|---|---|---|\n{rows}\n\n"
                   f"Why tests were skipped (all environmental):\n\n| Modules | Reason |\n|---|---|\n{skip_rows}\n")
p = ROOT / "PROJECT_STATUS.md"
t = p.read_text()
for k, v in blocks.items():
    t = re.sub(rf"(<!-- BEGIN:{k} -->).*?(<!-- END:{k} -->)", lambda m: m.group(1) + v + m.group(2), t, flags=re.S)
p.write_text(t)
print("PROJECT_STATUS.md updated:", T)
