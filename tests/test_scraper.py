from pathlib import Path

import pytest

from app.scraper import Listing, detect_max_page, parse_listing_page, parse_pay

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (FIXTURES / "listing_index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (FIXTURES / "job_detail.html").read_text(encoding="utf-8")


def test_parses_all_cards(index_html):
    listings = parse_listing_page(index_html)
    assert len(listings) == 50
    assert all(isinstance(l, Listing) for l in listings)


def test_every_listing_has_id_and_title(index_html):
    for l in parse_listing_page(index_html):
        assert l.source_id and l.source_id.isdigit()
        assert l.title


def test_first_card_fields(index_html):
    first = parse_listing_page(index_html)[0]
    assert first.source_id == "485939"
    # title is the specific job (last h5), not the category grouping (first h5)
    assert first.title == "OBDELAVA PODATKOV"
    assert first.location == "KRANJ"
    assert first.pay_raw == "9.04 €/h neto (10.50 €/h bruto)"
    assert first.pay_eur_hr == 9.04
    assert first.url.endswith("?isci=1&kljb=485939")
    assert "SAP" in first.description


def test_pay_by_agreement_is_unparsed(index_html):
    by_id = {l.source_id: l for l in parse_listing_page(index_html)}
    job = by_id["485935"]
    assert job.pay_raw == "PO DOGOVORU"
    assert job.pay_eur_hr is None


def test_absent_fields_stay_null(index_html):
    # company, region, posted_at are not shown on public cards
    for l in parse_listing_page(index_html):
        assert l.company is None
        assert l.region is None
        assert l.posted_at is None


def test_single_job_detail_view(detail_html):
    listings = parse_listing_page(detail_html)
    assert len(listings) == 1
    assert listings[0].source_id == "485939"


def test_pagination_detection(index_html):
    assert detect_max_page(index_html) == 55


def test_pagination_detection_absent():
    assert detect_max_page("<html><body>no pager here</body></html>") == 1


@pytest.mark.parametrize(
    "text, expected",
    [
        ("9,04 €/h neto", 9.04),
        ("10,64 €/h", 10.64),
        ("12.00 €/h neto (13.95 €/h bruto)", 12.00),  # picks the neto rate
        ("7,73 €/h bruto", 7.73),
        ("8 €/h", 8.0),
        ("5,50 EUR/h", 5.50),
        ("PO DOGOVORU", None),
        ("", None),
        (None, None),
    ],
)
def test_pay_parsing_table(text, expected):
    assert parse_pay(text) == expected
