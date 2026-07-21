import pytest

from app import create_app, cv, db, repo
from app.schemas import CoverNote, CVContent, Profile
from app.scraper import Listing

PROFILE_YAML = """
name: Ana Novak
contact: {email: ana@example.com, location: Maribor}
languages:
  - {name: Slovenian, level: Native}
  - {name: English, level: C1}
education:
  - {school: University of Maribor, program: Computer Science, years: 2022-present}
experience:
  - role: Student developer
    company: Kodni Atelje
    years: 2024-2025
    bullets: ["Built internal Flask tools"]
projects:
  - name: Napotnica
    stack: [Python, Flask]
    bullets: ["Local job scoring tool"]
skills: [Python, Flask, SQLite]
cv_language: sl
"""


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(PROFILE_YAML, encoding="utf-8")
    from app import matching
    return matching.load_profile(p)


@pytest.fixture
def job(tmp_path):
    conn = db.get_db(":memory:")
    repo.upsert_job(conn, Listing("485939", "OBDELAVA PODATKOV", None, "KRANJ", None,
                                  "9,04 €/h", 9.04, "https://x/?kljb=485939",
                                  "Delo z bazami in Python skripti.", None), now="t")
    row = repo.get_job(conn, 1)
    yield row
    conn.close()


class ScriptedLLM:
    """Returns queued CVContent/CoverNote objects and counts calls."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_json(self, messages, schema, temperature=0.0):
        self.calls += 1
        return self.replies.pop(0)


def honest_content():
    return CVContent(
        summary="Student razvijalec s Python izkušnjami.",
        experience=[{"company": "Kodni Atelje", "bullets": ["Gradil interna Flask orodja"]}],
        projects=[{"name": "Napotnica", "bullets": ["Lokalno orodje za ocenjevanje del"]}],
        skills=["Python", "Flask"],
    )


def invented_content():
    return CVContent(
        summary="Izkušen razvijalec.",
        experience=[{"company": "Acme", "bullets": ["Vodil ekipo"]}],
        projects=[{"name": "Napotnica", "bullets": ["Lokalno orodje"]}],
        skills=["Python"],
    )


# --------------------------------------------------------------------------- #
# The honesty guardrail
# --------------------------------------------------------------------------- #
def test_invented_employer_is_rejected_and_aborts_after_one_retry(profile, job):
    # profile has no company "Acme"; the model insists on it twice
    llm = ScriptedLLM(invented_content(), invented_content())
    with pytest.raises(cv.CVValidationError) as excinfo:
        cv.generate_cv_content(llm, profile, job)
    assert "Acme" in str(excinfo.value)
    assert llm.calls == 2  # original + exactly one retry, then abort


def test_retry_that_returns_honest_content_succeeds(profile, job):
    llm = ScriptedLLM(invented_content(), honest_content())
    content = cv.generate_cv_content(llm, profile, job)
    assert [e.company for e in content.experience] == ["Kodni Atelje"]
    assert llm.calls == 2


def test_honest_content_passes_first_time(profile, job):
    llm = ScriptedLLM(honest_content())
    content = cv.generate_cv_content(llm, profile, job)
    assert content.summary
    assert llm.calls == 1


def test_validator_flags_each_invented_kind(profile):
    bad = CVContent(
        summary="x",
        experience=[{"company": "Globex", "bullets": []}],
        projects=[{"name": "Ghost App", "bullets": []}],
        skills=["Rust"],
    )
    violations = cv.validate_cv_content(bad, profile)
    joined = " ".join(violations)
    assert "Globex" in joined and "Ghost App" in joined and "Rust" in joined
    assert len(violations) == 3


def test_validator_is_case_insensitive_and_allows_project_stack(profile):
    ok = CVContent(
        summary="x",
        experience=[{"company": "kodni atelje", "bullets": []}],
        projects=[{"name": "NAPOTNICA", "bullets": []}],
        skills=["python", "Flask"],  # Flask also appears in a project stack
    )
    assert cv.validate_cv_content(ok, profile) == []


# --------------------------------------------------------------------------- #
# Rendering and cover note
# --------------------------------------------------------------------------- #
def test_view_takes_role_and_years_from_profile_not_model(profile):
    view = cv.build_cv_view(honest_content(), profile)
    entry = view["experience"][0]
    assert entry["role"] == "Student developer"   # never supplied by the model
    assert entry["years"] == "2024-2025"
    assert view["labels"]["summary"] == "Povzetek"  # sl labels


def test_cv_html_renders_with_profile_facts(profile, tmp_path):
    app = create_app({"DB_PATH": str(tmp_path / "t.db"), "TESTING": True})
    with app.app_context():
        html = cv.render_cv_html(honest_content(), profile)
    assert "Ana Novak" in html
    assert "Kodni Atelje" in html
    assert "Povzetek" in html and "Izku" in html
    assert "Acme" not in html


def test_labels_cover_both_languages():
    expected = {"summary", "experience", "projects", "education", "skills", "languages"}
    for lang in ("sl", "en"):
        assert set(cv.LABELS[lang]) == expected
    assert cv.LABELS["en"]["languages"] == "Languages"


def test_english_cv_labels_languages_section(profile, tmp_path):
    english = profile.model_copy(update={"cv_language": "en"})
    app = create_app({"DB_PATH": str(tmp_path / "t.db"), "TESTING": True})
    with app.app_context():
        html = cv.render_cv_html(honest_content(), english)
    assert "Languages" in html
    assert "Jeziki" not in html


def test_cover_note_word_cap():
    CoverNote(note="beseda " * 120)  # exactly 120 is fine
    with pytest.raises(Exception):
        CoverNote(note="beseda " * 121)


def test_generate_cover_note_returns_text(profile, job):
    llm = ScriptedLLM(CoverNote(note="Pozdravljeni, zanima me delo OBDELAVA PODATKOV."))
    text = cv.generate_cover_note(llm, profile, job)
    assert "OBDELAVA PODATKOV" in text


def test_write_cover_note_uses_output_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "OUTPUT_DIR", tmp_path)
    from datetime import date
    path = cv.write_cover_note("besedilo", "485939", when=date(2026, 7, 18))
    assert path.name == "note_485939_2026-07-18.txt"
    assert path.read_text(encoding="utf-8") == "besedilo"


def test_cv_paths_naming(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "OUTPUT_DIR", tmp_path)
    from datetime import date
    pdf, note = cv.cv_paths("485939", when=date(2026, 7, 18))
    assert pdf.name == "cv_485939_2026-07-18.pdf"
    assert note.name == "note_485939_2026-07-18.txt"
