import json

import pytest

from app import db, matching, repo
from app.llm import LLMError
from app.schemas import JobExtract, JobScore, Profile
from app.scraper import Listing


def make_listing(source_id, description="delo z bazami"):
    return Listing(
        source_id=source_id, title="DELO", company=None, location="KRANJ", region=None,
        pay_raw="9,04 €/h", pay_eur_hr=9.04, url=f"https://example.test/?kljb={source_id}",
        description=description, posted_at=None,
    )


class FakeLLM:
    """Returns canned extract/score. A job whose text contains POISON blows up extract."""
    def __init__(self):
        self.calls = 0

    def complete_json(self, messages, schema, temperature=0.0):
        self.calls += 1
        user = messages[-1]["content"]
        if schema is JobExtract:
            if "POISON" in user:
                raise LLMError("bad extract")
            return JobExtract(category="IT", skills=["Python"], language="sl", work_mode="unknown")
        if schema is JobScore:
            return JobScore(score=80, fits=["python match", "flask"], gaps=[])
        raise AssertionError("unexpected schema")


@pytest.fixture
def conn():
    c = db.get_db(":memory:")
    yield c
    c.close()


def _seed(conn, ids_and_desc):
    for sid, desc in ids_and_desc:
        repo.upsert_job(conn, make_listing(sid, desc), now="2026-01-01T00:00:00+00:00")


def test_batch_survives_one_poisoned_job(conn):
    _seed(conn, [("1", "python flask"), ("2", "POISON pill"), ("3", "sql work")])
    out = matching.score_new_jobs(
        conn, profile_summary="Skills: Python", profile_hash="H1",
        llm=FakeLLM(), max_batch=40,
    )
    assert out == {"selected": 3, "scored": 2, "failed": 1}

    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["2"]["match_score"] is None            # poisoned job left unscored
    assert rows["2"]["match_profile_hash"] is None
    assert rows["1"]["match_score"] == 80
    assert rows["1"]["category"] == "IT"
    assert json.loads(rows["1"]["skills_json"]) == ["Python"]
    assert json.loads(rows["1"]["match_reasons_json"]) == {"fits": ["python match", "flask"], "gaps": []}
    assert rows["1"]["match_profile_hash"] == "H1"


def test_caching_skips_scored_rescoring_on_hash_and_force(conn):
    _seed(conn, [("1", "a"), ("2", "b")])
    matching.score_new_jobs(conn, profile_summary="s", profile_hash="H1", llm=FakeLLM())

    # same hash: both already scored -> nothing pending
    assert repo.jobs_needing_score(conn, "H1", limit=40) == []
    # changed hash: both need re-scoring
    assert len(repo.jobs_needing_score(conn, "H2", limit=40)) == 2
    # force: everything active regardless of hash
    assert len(repo.jobs_needing_score(conn, "H1", limit=40, force=True)) == 2


def test_batch_cap_limits_selection(conn):
    _seed(conn, [(str(i), "x") for i in range(5)])
    out = matching.score_new_jobs(conn, profile_summary="s", profile_hash="H1",
                                  llm=FakeLLM(), max_batch=2)
    assert out["selected"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM jobs WHERE match_score IS NOT NULL").fetchone()["c"] == 2


def test_failed_job_is_retried_next_batch(conn):
    _seed(conn, [("1", "POISON")])
    matching.score_new_jobs(conn, profile_summary="s", profile_hash="H1", llm=FakeLLM())
    # still pending for the same hash because it was left unscored
    assert len(repo.jobs_needing_score(conn, "H1", limit=40)) == 1


def test_profile_loading_and_hash(tmp_path):
    good = tmp_path / "p.yaml"
    good.write_text("name: Ana\nskills: [Python, SQL]\ncv_language: sl\n", encoding="utf-8")
    profile = matching.load_profile(good)
    assert isinstance(profile, Profile) and profile.name == "Ana"
    summary = matching.profile_summary(profile)
    assert "Ana" in summary and "Python" in summary

    h1 = matching.profile_fingerprint(good)
    good.write_text("name: Ana\nskills: [Python, SQL, Flask]\ncv_language: sl\n", encoding="utf-8")
    assert matching.profile_fingerprint(good) != h1  # content change flips the hash


def test_example_profile_loads():
    # guards against schema drift; a bare year like 2023 must coerce to a string
    profile = matching.load_profile("profile.example.yaml")
    assert profile.name
    assert profile.experience and profile.experience[-1].years == "2023"


def test_malformed_profile_raises_validation_error(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("contact: {email: a@b.c}\n", encoding="utf-8")  # missing required name
    with pytest.raises(Exception):
        matching.load_profile(bad)
