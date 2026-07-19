"""CSV and JSON export of a job list (whatever the caller filtered via repo.query_jobs).

CSV is written UTF-8 with a BOM so Excel opens Slovenian characters cleanly. JSON is
pretty-printed with an exported_at timestamp at the top level. Both use the same fixed
column set so the two files describe identical records.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Iterable, Optional

from .db import now_iso

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = _PROJECT_ROOT / "exports"

# company and region are never populated by this site, so they are left out of the
# export (the db columns stay, unused). See the location note in repo.query_jobs.
CSV_COLUMNS = [
    "source_id", "title", "location", "pay_raw", "pay_eur_hr",
    "url", "posted_at", "category", "match_score", "applied_at",
]


def _record(job) -> dict:
    return {col: job[col] for col in CSV_COLUMNS}


def to_csv_bytes(jobs: Iterable) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for job in jobs:
        writer.writerow(_record(job))
    # utf-8-sig prepends the BOM Excel needs for correct encoding detection.
    return buf.getvalue().encode("utf-8-sig")


def to_json_bytes(jobs: Iterable, exported_at: Optional[str] = None) -> bytes:
    payload = {
        "exported_at": exported_at or now_iso(),
        "jobs": [_record(job) for job in jobs],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _target(path: Optional[str | os.PathLike], suffix: str) -> Path:
    if path is not None:
        return Path(path)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "")
    return EXPORT_DIR / f"jobs_{stamp}.{suffix}"


def write_csv(jobs: Iterable, path: Optional[str | os.PathLike] = None) -> Path:
    target = _target(path, "csv")
    target.write_bytes(to_csv_bytes(jobs))
    return target


def write_json(jobs: Iterable, path: Optional[str | os.PathLike] = None,
               exported_at: Optional[str] = None) -> Path:
    target = _target(path, "json")
    target.write_bytes(to_json_bytes(jobs, exported_at=exported_at))
    return target
