"""Live scrape against the real site. Marked 'scrape', excluded by default.

Run explicitly with: pytest -m scrape
"""
import pytest

from app.scraper import scrape


@pytest.mark.scrape
def test_scrape_one_page():
    result = scrape(max_pages=1)
    assert result.listings, "expected at least one live listing"
    assert result.pages_scraped == 1
    first = result.listings[0]
    assert first.source_id.isdigit()
    assert first.title
    assert first.url.startswith("https://")
