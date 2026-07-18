import json

import pytest

from app import db, export, repo
from app.scraper import Listing


def make_listing(source_id, title, company=None, location="MARIBOR", pay_raw="9,04 €/h",
                 pay_eur_hr=9.04):
    return Listing(
        source_id=source_id, title=title, company=company, location=location, region="Podravska",
        pay_raw=pay_raw, pay_eur_hr=pay_eur_hr, url=f"https://example.test/?kljb={source_id}",
        description="opis dela", posted_at=None,
    )


@pytest.fixture
def conn():
    c = db.get_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def jobs(conn):
    # Slovenian characters exercise the encoding; one scored, one applied, one bare.
    repo.upsert_job(conn, make_listing("1", "RAČUNOVODSKA DELA", company="Črpalka d.o.o."), now="t")
    repo.upsert_job(conn, make_listing("2", "POMOČ V KUHINJI"), now="t")
    conn.execute("UPDATE jobs SET category='IT', match_score=82 WHERE source_id='1'")
    conn.execute("UPDATE jobs SET applied_at='2026-07-16T00:00:00+00:00' WHERE source_id='2'")
    return repo.query_jobs(conn, sort="date")


def test_csv_has_bom_and_exact_columns(jobs):
    raw = export.to_csv_bytes(jobs)
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    text = raw.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header == ",".join(export.CSV_COLUMNS)


def test_csv_preserves_slovenian_and_values(jobs):
    text = export.to_csv_bytes(jobs).decode("utf-8-sig")
    assert "RAČUNOVODSKA DELA" in text
    assert "Črpalka d.o.o." in text
    rows = list(__import__("csv").DictReader(text.splitlines()))
    by_id = {r["source_id"]: r for r in rows}
    assert by_id["1"]["match_score"] == "82"
    assert by_id["1"]["category"] == "IT"
    assert by_id["2"]["applied_at"] == "2026-07-16T00:00:00+00:00"
    assert by_id["2"]["match_score"] == ""  # unscored -> empty cell


def test_json_round_trips_with_timestamp(jobs):
    raw = export.to_json_bytes(jobs, exported_at="2026-07-16T12:00:00+00:00")
    payload = json.loads(raw)
    assert payload["exported_at"] == "2026-07-16T12:00:00+00:00"
    assert {j["source_id"] for j in payload["jobs"]} == {"1", "2"}
    job1 = next(j for j in payload["jobs"] if j["source_id"] == "1")
    assert job1["match_score"] == 82           # stays an int
    assert job1["company"] == "Črpalka d.o.o."
    assert set(job1.keys()) == set(export.CSV_COLUMNS)


def test_json_default_timestamp_present(jobs):
    payload = json.loads(export.to_json_bytes(jobs))
    assert payload["exported_at"]  # auto-filled when not provided


def test_writers_create_files(jobs, tmp_path):
    csv_path = export.write_csv(jobs, tmp_path / "out.csv")
    json_path = export.write_json(jobs, tmp_path / "out.json")
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(json_path.read_text(encoding="utf-8"))["jobs"]
