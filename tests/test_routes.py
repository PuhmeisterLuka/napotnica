import pytest

from app import create_app, db, repo, routes, scraper, matching
from app.scraper import Listing


def make_listing(source_id, title, location="MARIBOR"):
    return Listing(
        source_id=source_id, title=title, company=None, location=location, region=None,
        pay_raw="9,04 €/h", pay_eur_hr=9.04, url=f"https://example.test/?kljb={source_id}",
        description="opis dela", posted_at=None,
    )


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = db.get_db(db_path)
    repo.upsert_job(conn, make_listing("485939", "OBDELAVA PODATKOV", "KRANJ"), now="t")
    repo.upsert_job(conn, make_listing("485938", "POUČEVANJE", "LJUBLJANA RUDNIK"), now="t")
    conn.execute("UPDATE jobs SET category='IT', match_score=82 WHERE source_id='485939'")
    conn.commit()
    conn.close()
    return create_app({"DB_PATH": db_path, "PROFILE_PATH": "profile.example.yaml",
                       "TESTING": True, "SECRET_KEY": "t"})


@pytest.fixture
def client(app):
    return app.test_client()


def test_index_lists_jobs(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"OBDELAVA PODATKOV" in r.data
    assert b"napotnica" in r.data  # masthead


def test_index_location_filter(client):
    r = client.get("/?location=ljub")
    assert b"POU" in r.data           # LJUBLJANA RUDNIK job present
    assert b"OBDELAVA" not in r.data  # KRANJ job filtered out


def test_index_min_score_filter(client):
    r = client.get("/?min_score=75")
    assert b"OBDELAVA PODATKOV" in r.data  # scored 82
    assert b"POU" not in r.data            # unscored filtered out


def test_job_detail_and_missing(client):
    assert client.get("/job/1").status_code == 200
    assert client.get("/job/9999").status_code == 404


def test_mark_applied(client, app):
    r = client.post("/job/1/applied")
    assert r.status_code == 302
    conn = db.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT applied_at FROM jobs WHERE id=1").fetchone()["applied_at"] is not None
    conn.close()


def test_profile_page_ok(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert b"Ana Novak" in r.data


def test_profile_page_missing_file(app):
    app.config["PROFILE_PATH"] = "does_not_exist.yaml"
    r = app.test_client().get("/profile")
    assert r.status_code == 200
    assert b"not found" in r.data


def test_status_endpoints(client):
    assert client.get("/api/scrape/status").get_json()["status"] in {"none", "ok", "running", "failed"}
    assert client.get("/api/score/status").get_json()["running"] is False


def test_exports(client):
    csv = client.get("/export.csv")
    assert csv.status_code == 200
    assert csv.data.startswith(b"\xef\xbb\xbf")  # BOM
    assert "text/csv" in csv.headers["Content-Type"]
    js = client.get("/export.json")
    assert js.status_code == 200
    assert js.get_json()["jobs"]


def test_scrape_route_starts_background_without_network(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(scraper, "run_scrape", lambda **kw: called.__setitem__("n", called["n"] + 1))
    r = client.post("/scrape")
    assert r.status_code == 302
    if routes._scrape_thread:
        routes._scrape_thread.join(timeout=3)
    assert called["n"] == 1


def test_cv_route_starts_background_without_llm_or_browser(client, monkeypatch, tmp_path):
    from app import cv
    from types import SimpleNamespace
    monkeypatch.setattr(routes, "LLMClient", lambda: object())
    monkeypatch.setattr(cv, "generate_cv",
                        lambda llm, profile, job: SimpleNamespace(content=None, pdf_path=tmp_path / "cv.pdf"))
    r = client.post("/job/1/cv")
    assert r.status_code == 302
    if routes._doc_thread:
        routes._doc_thread.join(timeout=5)
    assert routes._doc_state["error"] is None, routes._doc_state["error"]
    assert routes._doc_state["cv_path"].endswith("cv.pdf")


def test_note_route_stores_text_for_copy_paste(client, monkeypatch, tmp_path):
    from app import cv
    monkeypatch.setattr(routes, "LLMClient", lambda: object())
    monkeypatch.setattr(cv, "generate_cover_note", lambda llm, profile, job: "Pozdravljeni, ...")
    monkeypatch.setattr(cv, "OUTPUT_DIR", tmp_path)
    r = client.post("/job/1/note")
    assert r.status_code == 302
    if routes._doc_thread:
        routes._doc_thread.join(timeout=5)
    assert routes._doc_state["error"] is None, routes._doc_state["error"]
    assert routes._doc_state["note_text"] == "Pozdravljeni, ..."
    # the note is shown on the detail page for copy-paste
    assert b"Pozdravljeni" in client.get("/job/1").data


def test_score_route_starts_background_without_network(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(matching, "run_scoring",
                        lambda conn, **kw: called.__setitem__("n", called["n"] + 1) or {"scored": 0})
    r = client.post("/score")
    assert r.status_code == 302
    if routes._score_thread:
        routes._score_thread.join(timeout=3)
    assert called["n"] == 1
