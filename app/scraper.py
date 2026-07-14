"""Scrape public student job listings from studentski-servis.com.

The listings live at ``/studenti/prosta-dela/`` and are reachable without login.
Each ad is an ``article.job-item`` whose ``data-jobid`` is the site šifra. The full
description sits on the card itself; the single-job "detail" view is the same card
filtered with ``?isci=1&kljb=<id>``. Pagination is ``?page=N`` and the total page
count is read from the ``data-page`` values on the pagination controls.

Parsing is pure stdlib (``html.parser``) so the parser tests run offline against
saved fixtures without a browser. Playwright is imported lazily inside ``scrape``.
"""
from __future__ import annotations

import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Callable, Optional

DEFAULT_BASE_URL = "https://www.studentski-servis.com"
LISTINGS_PATH = "/studenti/prosta-dela/"

# Elements that never have a closing tag in HTML5.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


@dataclass(frozen=True)
class Listing:
    source_id: str
    title: str
    company: Optional[str]
    location: Optional[str]
    region: Optional[str]
    pay_raw: Optional[str]
    pay_eur_hr: Optional[float]
    url: str
    description: Optional[str]
    posted_at: Optional[str]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScrapeResult:
    listings: list["Listing"]
    pages_scraped: int
    reached_end: bool  # True if we saw the last page; False if capped by max_pages


# --------------------------------------------------------------------------- #
# Minimal DOM: enough to find elements by tag/class and read their text.
# --------------------------------------------------------------------------- #
class _El:
    __slots__ = ("tag", "attrs", "classes", "parent", "content")

    def __init__(self, tag: str, attrs: dict, parent: Optional["_El"]):
        self.tag = tag
        self.attrs = attrs
        self.classes = set((attrs.get("class") or "").split())
        self.parent = parent
        self.content: list = []  # ordered mix of str and _El

    @property
    def children(self):
        return [c for c in self.content if isinstance(c, _El)]

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: "_El"):
            for item in node.content:
                if isinstance(item, str):
                    parts.append(item)
                else:
                    walk(item)

        walk(self)
        return " ".join("".join(parts).split())


class _DOMBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        el = _El(tag, dict(attrs), self._stack[-1])
        self._stack[-1].content.append(el)
        if tag not in _VOID:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        el = _El(tag, dict(attrs), self._stack[-1])
        self._stack[-1].content.append(el)

    def handle_endtag(self, tag):
        # Tolerant close: unwind to the nearest matching open tag.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].content.append(data)


def _parse(html: str) -> _El:
    b = _DOMBuilder()
    b.feed(html)
    return b.root


def _find_all(root: _El, tag: Optional[str] = None, class_: Optional[str] = None):
    out: list[_El] = []

    def walk(node: _El):
        for c in node.children:
            if (tag is None or c.tag == tag) and (class_ is None or class_ in c.classes):
                out.append(c)
            walk(c)

    walk(root)
    return out


def _find(root: _El, tag: Optional[str] = None, class_: Optional[str] = None) -> Optional[_El]:
    found = _find_all(root, tag, class_)
    return found[0] if found else None


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #
# Matches an hourly rate: "9.04 €/h", "10,64 €/h neto", "5,50 EUR/h". Comma or dot
# decimals; picks the first rate in the string (the neto figure on this site).
_PAY_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|eur)\s*/\s*h", re.IGNORECASE)
_SIFRA_RE = re.compile(r"\d{3,}")


def parse_pay(text: Optional[str]) -> Optional[float]:
    """Return the hourly euro rate as a float, or None when not a parseable rate."""
    if not text:
        return None
    m = _PAY_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def _icon_location(p: _El) -> bool:
    for use in _find_all(p, "use"):
        href = use.attrs.get("xlink:href") or use.attrs.get("href") or ""
        if "icon-location" in href:
            return True
    return False


def _location(card: _El) -> Optional[str]:
    for p in _find_all(card, "p"):
        if _icon_location(p):
            loc = p.text().strip()
            return loc or None
    return None


def _sifra_from_attrs(card: _El) -> Optional[str]:
    for li in _find_all(card, "li"):
        t = li.text()
        if "Šifra" in t:
            m = _SIFRA_RE.search(t)
            if m:
                return m.group(0)
    return None


def _listing_url(base_url: str, source_id: str) -> str:
    return f"{base_url.rstrip('/')}{LISTINGS_PATH}?isci=1&kljb={source_id}"


def parse_listing_page(html: str, base_url: str = DEFAULT_BASE_URL) -> list[Listing]:
    """Parse one listings page (index or single-job view) into Listing records."""
    root = _parse(html)
    listings: list[Listing] = []
    for card in _find_all(root, "article", "job-item"):
        source_id = card.attrs.get("data-jobid") or _sifra_from_attrs(card)
        if not source_id:
            continue
        h5s = _find_all(card, "h5")
        title = h5s[-1].text() if h5s else ""
        pay_li = _find(card, "li", "job-payment")
        pay_raw = pay_li.text() if pay_li else None
        desc_p = _find(card, "p", "description")
        description = desc_p.text() if desc_p else None
        listings.append(
            Listing(
                source_id=source_id,
                title=title,
                company=None,          # employer is not shown on public cards
                location=_location(card),
                region=None,           # region is only in the filter panel, not per-card
                pay_raw=pay_raw or None,
                pay_eur_hr=parse_pay(pay_raw),
                url=_listing_url(base_url, source_id),
                description=description,
                posted_at=None,        # posting date is not shown on public cards
            )
        )
    return listings


def detect_max_page(html: str) -> int:
    """Read the highest page number from the pagination controls (>=1)."""
    root = _parse(html)
    pages = []
    for el in _find_all(root, class_="page-link"):
        dp = el.attrs.get("data-page")
        if dp and dp.isdigit():
            pages.append(int(dp))
    return max(pages) if pages else 1


# --------------------------------------------------------------------------- #
# Live scraping (Playwright). Imported lazily so parsing stays browser-free.
# --------------------------------------------------------------------------- #
def scrape(
    base_url: Optional[str] = None,
    max_pages: Optional[int] = None,
    on_page: Optional[Callable[[int, list["Listing"]], None]] = None,
) -> ScrapeResult:
    """Fetch listings across pages, politely, and return unique Listing records.

    Single browser context, sequential navigation, a random 1.5-3s pause between
    page loads, default Playwright UA. Public pages only, no login.
    """
    base_url = (base_url or os.getenv("SCRAPE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    max_pages = max_pages or int(os.getenv("SCRAPE_MAX_PAGES", "10"))

    from playwright.sync_api import sync_playwright

    list_url = f"{base_url}{LISTINGS_PATH}"
    results: list[Listing] = []
    seen: set[str] = set()
    pages_scraped = 0
    reached_end = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        try:
            for n in range(1, max_pages + 1):
                page.goto(f"{list_url}?page={n}", wait_until="networkidle", timeout=60000)
                html = page.content()
                pages_scraped = n
                page_listings = parse_listing_page(html, base_url)
                if not page_listings:
                    reached_end = True
                    break
                fresh = [l for l in page_listings if l.source_id not in seen]
                for l in fresh:
                    seen.add(l.source_id)
                results.extend(fresh)
                if on_page:
                    on_page(n, fresh)
                if n >= detect_max_page(html):
                    reached_end = True
                    break
                time.sleep(random.uniform(1.5, 3.0))
        finally:
            browser.close()
    return ScrapeResult(listings=results, pages_scraped=pages_scraped, reached_end=reached_end)


def run_scrape(
    db_path=None,
    base_url: Optional[str] = None,
    max_pages: Optional[int] = None,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Scrape and persist through the repo, recording the run in scrape_runs.

    Deactivation of unseen jobs only happens when the pass reached the last page,
    so a run capped by SCRAPE_MAX_PAGES never wrongly retires jobs it never looked at.
    """
    from . import db as db_mod
    from . import repo

    conn = db_mod.get_db(db_path)
    repo.fail_stale_runs(conn, db_mod.now_iso())  # retire runs a hard kill left 'running'
    run_id = repo.record_scrape_start(conn, db_mod.now_iso())
    conn.commit()

    seen_ids: list[str] = []
    new_total = 0
    pages_done = 0

    def _persist_page(page_num: int, page_listings: list[Listing]) -> None:
        nonlocal new_total, pages_done
        pages_done = page_num
        now = db_mod.now_iso()
        page_new = 0
        for listing in page_listings:
            if repo.upsert_job(conn, listing, now):
                page_new += 1
            seen_ids.append(listing.source_id)
        new_total += page_new
        if on_page:
            on_page(page_num, page_new)  # genuinely new rows, not just parsed

    try:
        result = scrape(base_url, max_pages, on_page=_persist_page)
    except BaseException as exc:  # includes KeyboardInterrupt / SystemExit
        repo.record_scrape_finish(
            conn, run_id, status="failed", pages=pages_done,
            jobs_found=len(seen_ids), jobs_new=new_total,
            error=str(exc) or type(exc).__name__, now=db_mod.now_iso(),
        )
        conn.commit()
        conn.close()
        raise

    if result.reached_end:
        repo.deactivate_missing(conn, seen_ids)
    repo.record_scrape_finish(
        conn, run_id, status="ok", pages=result.pages_scraped,
        jobs_found=len(seen_ids), jobs_new=new_total, error=None, now=db_mod.now_iso(),
    )
    conn.commit()
    conn.close()
    return {
        "run_id": run_id,
        "pages": result.pages_scraped,
        "found": len(seen_ids),
        "new": new_total,
        "reached_end": result.reached_end,
    }


def _main() -> None:
    summary = run_scrape(on_page=lambda n, c: print(f"page {n}: {c} new listings", file=sys.stderr))
    print(
        f"run {summary['run_id']}: scraped {summary['found']} listings across "
        f"{summary['pages']} page(s), {summary['new']} new"
    )


if __name__ == "__main__":
    _main()
