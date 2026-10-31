#!/usr/bin/env python3
"""Write STAGE9_MANIFEST.json (file list + sha256) and build + verify netsim-lab-stage-9.zip. Usage: python scripts/make_stage9_zip.py OUT.zip"""
import hashlib, json, os, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", "dist", "build", ".mypy_cache", ".vite", "coverage"}
EXCLUDE_NAMES = {".env", ".DS_Store"}
EXCLUDE_SUFFIX = (".pyc", ".pem", ".key", ".sqlite3", ".db-wal", ".db-shm")
top = "netsim-lab"

def files():
    out = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = sorted(d for d in dn if d not in EXCLUDE_DIRS and not d.endswith(".egg-info"))
        for f in sorted(fn):
            if f in EXCLUDE_NAMES or f.endswith(EXCLUDE_SUFFIX) or f.endswith(".zip"):
                continue
            out.append(Path(dp, f).relative_to(ROOT).as_posix())
    return out

def main(dest):
    fl = [f for f in files() if f != "STAGE9_MANIFEST.json"]
    man = {"archive": "netsim-lab-stage-9.zip", "stage": 9, "top_level_directory": top + "/", "file_count": len(fl) + 1,
           "files": [{"path": f, "bytes": (ROOT / f).stat().st_size, "sha256": hashlib.sha256((ROOT / f).read_bytes()).hexdigest()} for f in fl],
           "note": "sha256 of every file except this manifest itself; excluded: node_modules, virtualenvs, caches, .git, build output, secrets (.env), *.zip."}
    (ROOT / "STAGE9_MANIFEST.json").write_text(json.dumps(man, indent=1) + "\n")
    allf = sorted(fl + ["STAGE9_MANIFEST.json"])
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in allf:
            z.write(ROOT / f, f"{top}/{f}")
    # ---- verify
    with zipfile.ZipFile(dest) as z:
        assert z.testzip() is None, "corrupt member"
        names = z.namelist()
        assert len(names) == len(allf)
        for need in ("frontend/package.json", "frontend/src/App.jsx", "frontend/src/pages/ComparePage.jsx", "backend/app/main.py", "backend/app/services/topology.py",
                     "STAGE9_MANIFEST.md", "docs/FRONTEND.md", "frontend/src/test/fixtures/api/exp_mbb.json", "backend/tests/test_api_topology.py"):
            assert f"{top}/{need}" in names, need
        bad = [n for n in names if any(p in n.split("/") for p in EXCLUDE_DIRS) or n.endswith((".pyc", "/.env"))]
        assert not bad, bad[:5]
        # byte-for-byte check of every member against disk
        for f in allf:
            assert hashlib.sha256(z.read(f"{top}/{f}")).hexdigest() == hashlib.sha256((ROOT / f).read_bytes()).hexdigest(), f
        fe = sum(n.startswith(f"{top}/frontend/") for n in names); be = sum(n.startswith(f"{top}/backend/") for n in names)
    print(f"OK {dest}: {len(names)} files ({fe} frontend, {be} backend), {Path(dest).stat().st_size/1e6:.2f} MB")

main(sys.argv[1])
