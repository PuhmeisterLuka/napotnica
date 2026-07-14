import pytest

from app import db, repo, scraper
from app.scraper import Listing, ScrapeResult


def make_listing(source_id):
    return Listing(
        source_id=source_id, title="DELO", company=None, location="KRANJ", region=None,
        pay_raw="9,04 €/h", pay_eur_hr=9.04, url=f"https://example.test/?kljb={source_id}",
        description="opis", posted_at=None,
    )


def test_on_page_reports_only_genuinely_new_rows(monkeypatch, tmp_path):
    listings = [make_listing("1"), make_listing("2")]

    def fake_scrape(base_url, max_pages, on_page):
        on_page(1, listings)  # scraper hands the page's parsed listings to run_scrape
        return ScrapeResult(listings=listings, pages_scraped=1, reached_end=True)

    monkeypatch.setattr(scraper, "scrape", fake_scrape)
    db_path = str(tmp_path / "t.db")

    first_calls = []
    scraper.run_scrape(db_path=db_path, on_page=lambda n, c: first_calls.append((n, c)))
    assert first_calls == [(1, 2)]  # both rows new on first run

    second_calls = []
    summary = scraper.run_scrape(db_path=db_path, on_page=lambda n, c: second_calls.append((n, c)))
    assert second_calls == [(1, 0)]  # nothing new on re-scrape
    assert summary["new"] == 0 and summary["found"] == 2


def test_interrupt_marks_run_failed_with_error(monkeypatch, tmp_path):
    def fake_scrape(base_url, max_pages, on_page):
        raise KeyboardInterrupt

    monkeypatch.setattr(scraper, "scrape", fake_scrape)
    db_path = str(tmp_path / "t.db")

    with pytest.raises(KeyboardInterrupt):
        scraper.run_scrape(db_path=db_path)

    conn = db.connect(db_path)
    run = repo.latest_scrape_run(conn)
    assert run["status"] == "failed"
    assert run["error"] == "KeyboardInterrupt"
    assert run["finished_at"] is not None
    conn.close()
