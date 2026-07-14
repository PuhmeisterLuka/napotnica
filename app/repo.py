"""All SQL for napotnica. Nothing else in the app touches the database directly.

Every query here is parameterized. Scrape-sourced columns and LLM-sourced columns
are kept separate: upserting a re-seen job refreshes what the scraper knows and never
clobbers category / skills / match_* written by the AI layer.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

# Columns the scraper owns. A re-seen job overwrites these; the LLM columns are left alone.
_SCRAPED_COLS = (
    "title", "company", "location", "region",
    "pay_raw", "pay_eur_hr", "url", "description", "posted_at",
)


# --------------------------------------------------------------------------- #
# Jobs: upsert, deactivate, read
# --------------------------------------------------------------------------- #
def upsert_job(conn: sqlite3.Connection, listing, now: str) -> bool:
    """Insert a new job or refresh an existing one. Returns True if it was new."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE source_id = ?", (listing.source_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO jobs (
                source_id, title, company, location, region,
                pay_raw, pay_eur_hr, url, description, posted_at,
                first_seen, last_seen, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                listing.source_id, listing.title, listing.company, listing.location,
                listing.region, listing.pay_raw, listing.pay_eur_hr, listing.url,
                listing.description, listing.posted_at, now, now,
            ),
        )
        return True

    set_clause = ", ".join(f"{c} = ?" for c in _SCRAPED_COLS)
    values = [getattr(listing, c) for c in _SCRAPED_COLS]
    values += [now]  # last_seen
    values.append(listing.source_id)
    conn.execute(
        f"UPDATE jobs SET {set_clause}, last_seen = ?, is_active = 1 WHERE source_id = ?",
        values,
    )
    return False


def deactivate_missing(conn: sqlite3.Connection, seen_source_ids: Iterable[str]) -> int:
    """Set is_active = 0 for jobs whose source_id was not seen in a full run."""
    seen = list(seen_source_ids)
    if not seen:
        cur = conn.execute("UPDATE jobs SET is_active = 0")
        return cur.rowcount
    placeholders = ",".join("?" * len(seen))
    cur = conn.execute(
        f"UPDATE jobs SET is_active = 0 WHERE source_id NOT IN ({placeholders})", seen
    )
    return cur.rowcount


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def query_jobs(
    conn: sqlite3.Connection,
    *,
    search: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    min_score: Optional[int] = None,
    min_pay: Optional[float] = None,
    hide_applied: bool = False,
    hide_blacklisted: bool = True,
    show_inactive: bool = False,
    sort: str = "score",
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []

    if not show_inactive:
        where.append("is_active = 1")
    if search:
        where.append("(title LIKE ? OR description LIKE ? OR company LIKE ?)")
        like = f"%{search}%"
        params += [like, like, like]
    if region:
        where.append("region = ?")
        params.append(region)
    if category:
        where.append("category = ?")
        params.append(category)
    if min_score is not None:
        where.append("match_score IS NOT NULL AND match_score >= ?")
        params.append(min_score)
    if min_pay is not None:
        where.append("pay_eur_hr IS NOT NULL AND pay_eur_hr >= ?")
        params.append(min_pay)
    if hide_applied:
        where.append("applied_at IS NULL")
    if hide_blacklisted:
        where.append("(company IS NULL OR company NOT IN (SELECT company FROM blacklist))")

    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # DESC puts NULLs last in SQLite, which is what we want for unscored jobs.
    if sort == "date":
        sql += " ORDER BY COALESCE(posted_at, first_seen) DESC, id DESC"
    else:
        sql += " ORDER BY match_score DESC, id DESC"
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# Blacklist
# --------------------------------------------------------------------------- #
def add_to_blacklist(conn: sqlite3.Connection, company: str) -> None:
    conn.execute("INSERT OR IGNORE INTO blacklist (company) VALUES (?)", (company,))


def remove_from_blacklist(conn: sqlite3.Connection, company: str) -> None:
    conn.execute("DELETE FROM blacklist WHERE company = ?", (company,))


def list_blacklist(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT company FROM blacklist ORDER BY company").fetchall()
    return [r["company"] for r in rows]


# --------------------------------------------------------------------------- #
# Scrape runs
# --------------------------------------------------------------------------- #
def record_scrape_start(conn: sqlite3.Connection, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, status) VALUES (?, 'running')", (now,)
    )
    return int(cur.lastrowid)


def record_scrape_finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    pages: int,
    jobs_found: int,
    jobs_new: int,
    error: Optional[str],
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE scrape_runs
        SET finished_at = ?, status = ?, pages = ?, jobs_found = ?, jobs_new = ?, error = ?
        WHERE id = ?
        """,
        (now, status, pages, jobs_found, jobs_new, error, run_id),
    )


def latest_scrape_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def fail_stale_runs(conn: sqlite3.Connection, now: str, error: str = "interrupted") -> int:
    """Mark any run still 'running' as failed. A hard kill can't run cleanup, so
    this is called at startup to retire rows the previous process left behind."""
    cur = conn.execute(
        "UPDATE scrape_runs SET status = 'failed', finished_at = ?, error = ? WHERE status = 'running'",
        (now, error),
    )
    return cur.rowcount
