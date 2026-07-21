"""Tailored CV + cover note for one job, built only from facts in profile.yaml.

The honesty guardrail is the point of this module. The model may choose which profile
entries to use, put them in order, and reword their bullets. It may not introduce a
company, project, school or skill that is not in the profile. Everything it returns is
checked against the profile; a violation gets one retry naming the offence, and then
the attempt is aborted rather than written to a document someone would send out.

Roles, years, schools and contact details never come from the model at all: they are
rendered straight from the profile, so they cannot drift.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from flask import render_template

from .llm import LLMClient
from .schemas import CoverNote, CVContent, Profile

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = _PROJECT_ROOT / "output"
FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"

# Section labels per CV language.
LABELS = {
    "sl": {"summary": "Povzetek", "experience": "Izkušnje", "projects": "Projekti",
           "education": "Izobrazba", "skills": "Znanja", "languages": "Jeziki"},
    "en": {"summary": "Summary", "experience": "Experience", "projects": "Projects",
           "education": "Education", "skills": "Skills", "languages": "Languages"},
}


class CVValidationError(Exception):
    """Raised when the model invented a fact that is not in the profile."""


@dataclass(frozen=True)
class GeneratedCV:
    content: CVContent
    pdf_path: Path


def _fold(s: str) -> str:
    return s.strip().casefold()


def profile_facts(profile: Profile) -> dict[str, set[str]]:
    """The only names allowed to appear in generated content."""
    stacks = {_fold(item) for p in profile.projects for item in p.stack}
    return {
        "companies": {_fold(e.company) for e in profile.experience},
        "projects": {_fold(p.name) for p in profile.projects},
        "schools": {_fold(ed.school) for ed in profile.education},
        "skills": {_fold(s) for s in profile.skills} | stacks,
    }


def validate_cv_content(content: CVContent, profile: Profile) -> list[str]:
    """Return a list of invented facts. Empty list means the content is honest."""
    facts = profile_facts(profile)
    violations: list[str] = []

    for entry in content.experience:
        if _fold(entry.company) not in facts["companies"]:
            violations.append(f"company {entry.company!r} is not in the profile")
    for project in content.projects:
        if _fold(project.name) not in facts["projects"]:
            violations.append(f"project {project.name!r} is not in the profile")
    for skill in content.skills:
        if _fold(skill) not in facts["skills"]:
            violations.append(f"skill {skill!r} is not in the profile")
    return violations


_CV_SYSTEM = (
    "You tailor a student's CV to one job advert. You may ONLY select, order and reword "
    "material that already exists in the profile. Never invent an employer, project, "
    "school or skill, and never add one that is missing from the profile.\n"
    "Reply with ONLY a JSON object: summary (2-3 sentences, targeted at this job), "
    "experience (at most 3 items, each {company, bullets}; company must be copied "
    "verbatim from the profile), projects (at most 3 items, each {name, bullets}; name "
    "copied verbatim), skills (profile skills ordered by relevance to this job). "
    "Bullets may be reworded but must stay truthful to the profile."
)


def _job_block(job) -> str:
    parts = [f"Title: {job['title']}"]
    if job["location"]:
        parts.append(f"Location: {job['location']}")
    if job["category"]:
        parts.append(f"Category: {job['category']}")
    if job["description"]:
        parts.append(f"Description: {job['description']}")
    return "\n".join(parts)


def _profile_block(profile: Profile) -> str:
    lines = [f"Name: {profile.name}"]
    if profile.skills:
        lines.append("Skills: " + ", ".join(profile.skills))
    for e in profile.experience:
        lines.append(f"Experience entry - company: {e.company} | role: {e.role}"
                     + (f" | years: {e.years}" if e.years else ""))
        lines += [f"    bullet: {b}" for b in e.bullets]
    for p in profile.projects:
        lines.append(f"Project entry - name: {p.name}"
                     + (f" | stack: {', '.join(p.stack)}" if p.stack else ""))
        lines += [f"    bullet: {b}" for b in p.bullets]
    for ed in profile.education:
        lines.append(f"Education entry - school: {ed.school} | program: {ed.program or ''}")
    return "\n".join(lines)


def generate_cv_content(llm: LLMClient, profile: Profile, job) -> CVContent:
    """Generate CV content and refuse to return anything that invents a fact."""
    language = profile.cv_language
    messages = [
        {"role": "system", "content": _CV_SYSTEM},
        {"role": "user", "content": (
            f"Write the CV content in language '{language}'.\n\n"
            f"PROFILE:\n{_profile_block(profile)}\n\nJOB:\n{_job_block(job)}"
        )},
    ]

    content = llm.complete_json(messages, CVContent, temperature=0.0)
    violations = validate_cv_content(content, profile)
    if not violations:
        return content

    # Exactly one retry, naming what was invented.
    retry = messages + [
        {"role": "assistant", "content": content.model_dump_json()},
        {"role": "user", "content": (
            "That reply invented facts that are not in the profile: "
            + "; ".join(violations)
            + ". Use only companies, projects and skills copied verbatim from the "
              "profile above. Reply again with ONLY a valid JSON object."
        )},
    ]
    content = llm.complete_json(retry, CVContent, temperature=0.0)
    violations = validate_cv_content(content, profile)
    if violations:
        raise CVValidationError(
            "CV generation aborted, the model kept inventing facts: " + "; ".join(violations)
        )
    return content


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def build_cv_view(content: CVContent, profile: Profile) -> dict:
    """Join the model's selection back onto the profile's own facts."""
    by_company = {_fold(e.company): e for e in profile.experience}
    by_project = {_fold(p.name): p for p in profile.projects}

    experience = []
    for entry in content.experience:
        source = by_company[_fold(entry.company)]
        experience.append({
            "company": source.company,   # from the profile, not the model
            "role": source.role,
            "years": source.years,
            "bullets": entry.bullets or source.bullets,
        })
    projects = []
    for project in content.projects:
        source = by_project[_fold(project.name)]
        projects.append({
            "name": source.name,
            "stack": source.stack,
            "bullets": project.bullets or source.bullets,
        })
    return {
        "profile": profile,
        "summary": content.summary,
        "experience": experience,
        "projects": projects,
        "skills": content.skills or profile.skills,
        "labels": LABELS.get(profile.cv_language, LABELS["sl"]),
        "fonts_base": FONT_DIR.as_uri(),
    }


def render_cv_html(content: CVContent, profile: Profile) -> str:
    return render_template("cv_document.html", **build_cv_view(content, profile))


# A4 minus 18mm margins, in CSS px at 96dpi: 174mm wide, 261mm tall.
PRINTABLE_W = 658
PRINTABLE_H = 987
_MARGIN = {"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"}


def render_pdf(content: CVContent, profile: Profile, pdf_path: Path) -> CVContent:
    """Print the CV to one A4 page, returning the content actually rendered.

    If it does not fit, the lowest-relevance project (the last one, since the model
    orders them) is dropped and it is measured again. Type size is never reduced.
    """
    from playwright.sync_api import sync_playwright

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_html = pdf_path.with_suffix(".html")
    current = content
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": PRINTABLE_W, "height": PRINTABLE_H})
            page.emulate_media(media="print")
            while True:
                tmp_html.write_text(render_cv_html(current, profile), encoding="utf-8")
                # file:// so the self-hosted font files resolve
                page.goto(tmp_html.as_uri(), wait_until="load")
                page.evaluate("document.fonts.ready")
                overflows = page.evaluate(
                    f"document.documentElement.scrollHeight > {PRINTABLE_H}"
                )
                if not overflows or not current.projects:
                    break
                current = current.model_copy(update={"projects": current.projects[:-1]})
            page.pdf(path=str(pdf_path), format="A4", print_background=True, margin=_MARGIN)
            browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)
    return current


def cv_paths(source_id: str, when: Optional[date] = None) -> tuple[Path, Path]:
    stamp = (when or date.today()).isoformat()
    return (OUTPUT_DIR / f"cv_{source_id}_{stamp}.pdf",
            OUTPUT_DIR / f"note_{source_id}_{stamp}.txt")


def generate_cv(llm: LLMClient, profile: Profile, job) -> GeneratedCV:
    content = generate_cv_content(llm, profile, job)
    pdf_path, _ = cv_paths(job["source_id"])
    rendered = render_pdf(content, profile, pdf_path)
    return GeneratedCV(content=rendered, pdf_path=pdf_path)


# --------------------------------------------------------------------------- #
# Cover note
# --------------------------------------------------------------------------- #
_NOTE_SYSTEM = (
    "You write a short cover note for a student job application. Reply with ONLY a JSON "
    "object {\"note\": \"...\"}. Rules: at most 120 words; plain text; name the actual "
    "job title; give exactly one genuine reason the candidate fits, taken from the "
    "profile; no flattery, no invented enthusiasm, no facts absent from the profile."
)


def generate_cover_note(llm: LLMClient, profile: Profile, job) -> str:
    messages = [
        {"role": "system", "content": _NOTE_SYSTEM},
        {"role": "user", "content": (
            f"Write the note in language '{profile.cv_language}'.\n\n"
            f"PROFILE:\n{_profile_block(profile)}\n\nJOB:\n{_job_block(job)}"
        )},
    ]
    return llm.complete_json(messages, CoverNote, temperature=0.0).note


def write_cover_note(text: str, source_id: str, when: Optional[date] = None) -> Path:
    _, note_path = cv_paths(source_id, when)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(text, encoding="utf-8")
    return note_path
