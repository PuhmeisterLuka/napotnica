"""SQLite connection and schema. Migrations are plain CREATE IF NOT EXISTS."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "napotnica.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT UNIQUE NOT NULL,
    title              TEXT NOT NULL,
    company            TEXT,
    location           TEXT,
    region             TEXT,
    pay_raw            TEXT,
    pay_eur_hr         REAL,
    url                TEXT NOT NULL,
    description        TEXT,
    posted_at          TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    is_active          INTEGER NOT NULL DEFAULT 1,
    category           TEXT,
    skills_json        TEXT,
    language           TEXT,
    match_score        INTEGER,
    match_reasons_json TEXT,
    match_profile_hash TEXT,
    applied_at         TEXT,
    notes              TEXT
);

CREATE TABLE IF NOT EXISTS blacklist (
    company TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    pages       INTEGER,
    jobs_found  INTEGER,
    jobs_new    INTEGER,
    error       TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Optional[str | os.PathLike] = None) -> sqlite3.Connection:
    path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def get_db(db_path: Optional[str | os.PathLike] = None) -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn
