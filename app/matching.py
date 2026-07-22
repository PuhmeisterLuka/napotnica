"""Extract + score pipeline.

Stage A pulls structured metadata from a job ad; Stage B scores the profile against
that job. Results are cached by a sha256 of profile.yaml, so a job is only (re)scored
when it is new, when the profile file changes, or when the user forces it. A single
bad listing is caught and marked unscored; it never aborts the batch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

import yaml

from . import repo
from .llm import LLMClient
from .schemas import JobExtract, JobScore, Profile

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = _PROJECT_ROOT / "profile.yaml"


# --------------------------------------------------------------------------- #
# Profile loading, hashing, summarising
# --------------------------------------------------------------------------- #
def load_profile(path: Optional[str | os.PathLike] = None) -> Profile:
    path = Path(path) if path else DEFAULT_PROFILE_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile.model_validate(data)


def profile_fingerprint(path: Optional[str | os.PathLike] = None) -> str:
    path = Path(path) if path else DEFAULT_PROFILE_PATH
    return hashlib.sha256(path.read_bytes()).hexdigest()


def profile_summary(profile: Profile) -> str:
    lines = [f"Name: {profile.name}"]
    if profile.skills:
        lines.append("Skills: " + ", ".join(profile.skills))
    if profile.languages:
        lines.append("Languages: " + ", ".join(
            f"{l.name} ({l.level})" if l.level else l.name for l in profile.languages
        ))
    for e in profile.experience:
        head = f"Experience: {e.role} at {e.company}"
        if e.years:
            head += f" ({e.years})"
        lines.append(head)
        lines += [f"  - {b}" for b in e.bullets]
    for p in profile.projects:
        stack = f" [{', '.join(p.stack)}]" if p.stack else ""
        lines.append(f"Project: {p.name}{stack}")
        lines += [f"  - {b}" for b in p.bullets]
    for ed in profile.education:
        lines.append(f"Education: {ed.program or ''} at {ed.school}".strip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The two LLM stages
# --------------------------------------------------------------------------- #
_EXTRACT_SYSTEM = (
    "You extract structured metadata from a single Slovenian student job ad. "
    "Reply with ONLY a JSON object with keys: "
    "category (short English label such as IT, Hospitality, Sales, Logistics, "
    "Education, Admin, Manual labor, Healthcare), "
    "skills (array of concrete skills or tools the ad asks for), "
    "language ('sl' or 'en', the language the ad is written in), "
    "work_mode ('remote', 'onsite', or 'unknown')."
)

_SCORE_SYSTEM = (
    "You score how well a student's profile fits a job, for a student job market in "
    "Slovenia. Reply with ONLY a JSON object with keys: score (integer 0-100), "
    "fits (2-3 short strings, concrete reasons the profile fits), gaps (0-2 short "
    "strings, concrete missing requirements). Calibrate: 75+ strong, 50-74 partial, "
    "below 50 weak. Judge only from the given profile and job, invent nothing."
)


def _job_text(job: sqlite3.Row) -> str:
    parts = [f"Title: {job['title']}"]
    if job["location"]:
        parts.append(f"Location: {job['location']}")
    if job["description"]:
        parts.append(f"Description: {job['description']}")
    return "\n".join(parts)


def extract_job(llm: LLMClient, job: sqlite3.Row) -> JobExtract:
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": _job_text(job)},
    ]
    return llm.complete_json(messages, JobExtract, temperature=0.0)


def score_job(llm: LLMClient, summary: str, extract: JobExtract, job: sqlite3.Row) -> JobScore:
    job_block = (
        f"category: {extract.category}\n"
        f"required skills: {', '.join(extract.skills) or 'none stated'}\n"
        f"work mode: {extract.work_mode}\n"
        f"{_job_text(job)}"
    )
    messages = [
        {"role": "system", "content": _SCORE_SYSTEM},
        {"role": "user", "content": f"PROFILE:\n{summary}\n\nJOB:\n{job_block}"},
    ]
    return llm.complete_json(messages, JobScore, temperature=0.0)


# --------------------------------------------------------------------------- #
# Batch runner
# --------------------------------------------------------------------------- #
def score_new_jobs(
    conn: sqlite3.Connection,
    *,
    profile_summary: str,
    profile_hash: str,
    llm: LLMClient,
    max_batch: int = 40,
    force: bool = False,
) -> dict:
    rows = repo.jobs_needing_score(conn, profile_hash, limit=max_batch, force=force)
    scored = 0
    failed = 0
    for job in rows:
        try:
            extract = extract_job(llm, job)
            repo.save_job_extract(
                conn, job["id"], extract.category,
                json.dumps(extract.skills, ensure_ascii=False), extract.language,
            )
            result = score_job(llm, profile_summary, extract, job)
            reasons = json.dumps({"fits": result.fits, "gaps": result.gaps}, ensure_ascii=False)
            repo.save_job_score(conn, job["id"], result.score, reasons, profile_hash)
            scored += 1
        except Exception as exc:
            # One poisoned listing must never kill the batch.
            logger.warning("scoring failed for job %s: %s", job["id"], exc)
            repo.mark_job_unscored(conn, job["id"])
            failed += 1
    conn.commit()
    return {"selected": len(rows), "scored": scored, "failed": failed}


def run_scoring(
    conn: sqlite3.Connection,
    *,
    profile_path: Optional[str | os.PathLike] = None,
    llm: Optional[LLMClient] = None,
    max_batch: Optional[int] = None,
    force: bool = False,
) -> dict:
    """Load the profile, then score the pending batch. Convenience wrapper for the UI/CLI."""
    profile = load_profile(profile_path)
    summary = profile_summary(profile)
    fingerprint = profile_fingerprint(profile_path)
    max_batch = max_batch or int(os.getenv("MAX_SCORE_BATCH", "40"))
    llm = llm or LLMClient()
    return score_new_jobs(
        conn, profile_summary=summary, profile_hash=fingerprint,
        llm=llm, max_batch=max_batch, force=force,
    )
