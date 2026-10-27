"""Read raw run files without repairing or inventing anything."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from .schema import COLUMNS, INT_COLUMNS


@dataclass
class LoadResult:
    rows: list = field(default_factory=list)
    skipped_rows: int = 0          # malformed rows (wrong field count / non-integer), dropped as-is
    truncated_tail: bool = False   # file did not end with a newline -> last (partial) line dropped
    present: bool = True


def read_csv(path: Path, name: str) -> LoadResult:
    path = Path(path)
    if not path.exists():
        return LoadResult(present=False)
    text = path.read_text(errors="replace")
    res = LoadResult()
    if not text:
        return res
    if not text.endswith("\n"):
        # the writer always terminates rows with \n, so an unterminated tail is a partial write
        text = text[: text.rfind("\n") + 1]
        res.truncated_tail = True
    cols, ints = COLUMNS[name], INT_COLUMNS[name]
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or tuple(header) != cols:
        res.skipped_rows += 0 if header is None else 1
        res.present = header is not None
        if tuple(header or ()) != cols:
            raise ValueError(f"{path}: unexpected header {header!r}, expected {list(cols)}")
    for row in reader:
        if len(row) != len(cols):
            res.skipped_rows += 1
            continue
        try:
            res.rows.append({c: (int(v) if c in ints else v) for c, v in zip(cols, row)})
        except ValueError:
            res.skipped_rows += 1
    return res


def read_json(path: Path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None
