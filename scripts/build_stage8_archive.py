#!/usr/bin/env python3
"""Build netsim-lab-stage-8.zip from the working tree, write STAGE8_MANIFEST.json, then VERIFY the archive.

    python scripts/build_stage8_archive.py [OUTPUT.zip]

Excluded: .git, virtualenvs, node_modules, caches, bytecode, .env, runtime data (data/*), previous archives, temp files.
Verification: zip integrity (testzip), member list == manifest list, sha256 of every member == manifest. Exit code 1 on any mismatch."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = "netsim-lab"
EXCLUDE_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", ".idea", ".vscode"}
EXCLUDE_NAMES = {".env", ".DS_Store"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".zip", ".tmp", ".swp", ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")
MANIFEST = "STAGE8_MANIFEST.json"


def keep(p: Path) -> bool:
    rel = p.relative_to(ROOT)
    parts = rel.parts
    if any(x in EXCLUDE_DIRS or x.endswith(".egg-info") for x in parts[:-1]) or p.name in EXCLUDE_NAMES or p.name.endswith(EXCLUDE_SUFFIX):
        return False
    if parts[0] == "data" and p.name != ".keep_dir":
        return False
    return p.is_file() and not p.is_symlink() and rel.as_posix() != MANIFEST


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "netsim-lab-stage-8.zip"
    files = sorted((p for p in ROOT.rglob("*") if keep(p)), key=lambda p: p.relative_to(ROOT).as_posix())
    entries = [{"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p.read_bytes())} for p in files]
    manifest = {"archive": out.name, "stage": 8, "top_level_directory": TOP, "file_count": len(entries) + 1, "files": entries,
                "note": "this manifest lists every other file; the archive's own SHA-256 is reported outside the archive. Validation status: STAGE8_MANIFEST.md."}
    mtext = json.dumps(manifest, indent=1) + "\n"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, e in zip(files, entries):
            z.write(p, f"{TOP}/{e['path']}")
        z.writestr(f"{TOP}/{MANIFEST}", mtext)
    # ---- verification ----
    bad = []
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            bad.append(f"corrupt member {z.testzip()}")
        names = [n for n in z.namelist() if not n.endswith("/")]
        expect = sorted([f"{TOP}/{e['path']}" for e in entries] + [f"{TOP}/{MANIFEST}"])
        if sorted(names) != expect:
            bad.append(f"member list differs from manifest: {set(names) ^ set(expect)}")
        listed = json.loads(z.read(f"{TOP}/{MANIFEST}"))["files"]
        for e in listed:
            if sha(z.read(f"{TOP}/{e['path']}")) != e["sha256"]:
                bad.append(f"sha256 mismatch {e['path']}")
        for n in names:
            if any(x in n.split("/") for x in EXCLUDE_DIRS) or n.endswith((".pyc", ".env")):
                bad.append(f"excluded kind present: {n}")
    print(f"{out}  {out.stat().st_size} bytes  {len(entries) + 1} files  archive sha256 {sha(out.read_bytes())}")
    print("VERIFIED: integrity, member list and every sha256 match the manifest" if not bad else "VERIFICATION FAILED:\n  " + "\n  ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
