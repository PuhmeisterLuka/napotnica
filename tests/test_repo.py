import pytest

from app import db, repo
from app.scraper import Listing


def make_listing(source_id="1001", title="OBDELAVA PODATKOV", company=None,
                 location="KRANJ", region=None, pay_raw="9,04 €/h", pay_eur_hr=9.04,
                 description="delo z bazami", posted_at=None):
    return Listing(
        source_id=source_id, title=title, company=company, location=location,
        region=region, pay_raw=pay_raw, pay_eur_hr=pay_eur_hr,
        url=f"https://example.test/?kljb={source_id}", description=description,
        posted_at=posted_at,
    )


@pytest.fixture
def conn():
    c = db.get_db(":memory:")
    yield c
    c.close()


def test_upsert_inserts_then_updates_same_row(conn):
    assert repo.upsert_job(conn, make_listing(title="OLD"), now="2026-01-01T00:00:00+00:00") is True
    assert repo.upsert_job(conn, make_listing(title="NEW"), now="2026-02-02T00:00:00+00:00") is False

    rows = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "NEW"                       # changed field refreshed
    assert row["first_seen"] == "2026-01-01T00:00:00+00:00"  # first_seen preserved
    assert row["last_seen"] == "2026-02-02T00:00:00+00:00"   # last_seen advanced


def test_upsert_does_not_clobber_llm_columns(conn):
    repo.upsert_job(conn, make_listing(), now="2026-01-01T00:00:00+00:00")
    conn.execute("UPDATE jobs SET match_score = 88, category = 'IT' WHERE source_id = '1001'")
    repo.upsert_job(conn, make_listing(title="REWORDED"), now="2026-01-02T00:00:00+00:00")
    row = conn.execute("SELECT * FROM jobs WHERE source_id = '1001'").fetchone()
    assert row["title"] == "REWORDED"
    assert row["match_score"] == 88
    assert row["category"] == "IT"


def test_deactivate_missing_retires_unseen(conn):
    for sid in ("1", "2", "3"):
        repo.upsert_job(conn, make_listing(source_id=sid), now="2026-01-01T00:00:00+00:00")
    repo.deactivate_missing(conn, ["1", "3"])
    active = {r["source_id"] for r in conn.execute("SELECT source_id FROM jobs WHERE is_active = 1")}
    assert active == {"1", "3"}
    assert conn.execute("SELECT is_active FROM jobs WHERE source_id = '2'").fetchone()["is_active"] == 0


def test_deactivate_missing_empty_retires_all(conn):
    repo.upsert_job(conn, make_listing(source_id="1"), now="2026-01-01T00:00:00+00:00")
    repo.deactivate_missing(conn, [])
    assert conn.execute("SELECT COUNT(*) c FROM jobs WHERE is_active = 1").fetchone()["c"] == 0


def test_reseen_job_reactivates(conn):
    repo.upsert_job(conn, make_listing(source_id="1"), now="2026-01-01T00:00:00+00:00")
    repo.deactivate_missing(conn, [])
    repo.upsert_job(conn, make_listing(source_id="1"), now="2026-01-05T00:00:00+00:00")
    assert conn.execute("SELECT is_active FROM jobs WHERE source_id = '1'").fetchone()["is_active"] == 1


def test_blacklist_filtering(conn):
    repo.upsert_job(conn, make_listing(source_id="1", company="Acme"), now="2026-01-01T00:00:00+00:00")
    repo.upsert_job(conn, make_listing(source_id="2", company="Globex"), now="2026-01-01T00:00:00+00:00")
    repo.upsert_job(conn, make_listing(source_id="3", company=None), now="2026-01-01T00:00:00+00:00")
    repo.add_to_blacklist(conn, "Acme")

    kept = {r["source_id"] for r in repo.query_jobs(conn, hide_blacklisted=True)}
    assert kept == {"2", "3"}  # Acme hidden, null company kept
    all_rows = {r["source_id"] for r in repo.query_jobs(conn, hide_blacklisted=False)}
    assert all_rows == {"1", "2", "3"}
    assert repo.list_blacklist(conn) == ["Acme"]


def test_filter_combinations(conn):
    repo.upsert_job(conn, make_listing(source_id="1", location="KRANJ", pay_eur_hr=9.0), now="t")
    repo.upsert_job(conn, make_listing(source_id="2", location="KRANJ", pay_eur_hr=15.0,
                                       title="RAZVOJ PROGRAMSKE OPREME"), now="t")
    repo.upsert_job(conn, make_listing(source_id="3", location="LJUBLJANA", pay_eur_hr=12.0), now="t")
    conn.execute("UPDATE jobs SET region = 'Gorenjska' WHERE location = 'KRANJ'")
    conn.execute("UPDATE jobs SET match_score = 80 WHERE source_id = '2'")
    conn.execute("UPDATE jobs SET applied_at = 't' WHERE source_id = '1'")

    assert {r["source_id"] for r in repo.query_jobs(conn, region="Gorenjska")} == {"1", "2"}
    assert {r["source_id"] for r in repo.query_jobs(conn, min_pay=12.0)} == {"2", "3"}
    assert {r["source_id"] for r in repo.query_jobs(conn, min_score=50)} == {"2"}
    assert {r["source_id"] for r in repo.query_jobs(conn, search="RAZVOJ")} == {"2"}
    assert {r["source_id"] for r in repo.query_jobs(conn, hide_applied=True)} == {"2", "3"}
    # combined: Gorenjska AND min_pay 12 -> only job 2
    assert {r["source_id"] for r in repo.query_jobs(conn, region="Gorenjska", min_pay=12.0)} == {"2"}


def test_query_hides_inactive_by_default(conn):
    repo.upsert_job(conn, make_listing(source_id="1"), now="t")
    repo.upsert_job(conn, make_listing(source_id="2"), now="t")
    repo.deactivate_missing(conn, ["1"])
    assert {r["source_id"] for r in repo.query_jobs(conn)} == {"1"}
    assert {r["source_id"] for r in repo.query_jobs(conn, show_inactive=True)} == {"1", "2"}


def test_scrape_run_lifecycle(conn):
    run_id = repo.record_scrape_start(conn, "2026-01-01T00:00:00+00:00")
    assert repo.latest_scrape_run(conn)["status"] == "running"
    repo.record_scrape_finish(conn, run_id, status="ok", pages=2, jobs_found=100,
                              jobs_new=7, error=None, now="2026-01-01T00:05:00+00:00")
    run = repo.latest_scrape_run(conn)
    assert run["status"] == "ok"
    assert run["pages"] == 2 and run["jobs_found"] == 100 and run["jobs_new"] == 7
    assert run["finished_at"] == "2026-01-01T00:05:00+00:00"


def test_fail_stale_runs_marks_running_as_failed(conn):
    # a previous process left a run 'running' (never finished)
    repo.record_scrape_start(conn, "2026-01-01T00:00:00+00:00")
    # a cleanly finished run must not be touched
    done = repo.record_scrape_start(conn, "2026-01-02T00:00:00+00:00")
    repo.record_scrape_finish(conn, done, status="ok", pages=1, jobs_found=1,
                              jobs_new=1, error=None, now="2026-01-02T00:01:00+00:00")

    changed = repo.fail_stale_runs(conn, "2026-01-03T00:00:00+00:00")
    assert changed == 1

    rows = {r["id"]: r for r in conn.execute("SELECT * FROM scrape_runs")}
    stale = rows[1]
    assert stale["status"] == "failed"
    assert stale["error"] == "interrupted"
    assert stale["finished_at"] == "2026-01-03T00:00:00+00:00"
    assert rows[done]["status"] == "ok"  # finished run untouched
