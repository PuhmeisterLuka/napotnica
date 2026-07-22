"""Pydantic models: the profile file and the JSON returned by each LLM stage."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Profile (profile.yaml)
# --------------------------------------------------------------------------- #
class _ProfileModel(BaseModel):
    # A bare year or phone in YAML parses as a number; accept it as a string.
    model_config = ConfigDict(coerce_numbers_to_str=True)


class Contact(_ProfileModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    github: Optional[str] = None


class LanguageItem(_ProfileModel):
    name: str
    level: Optional[str] = None


class EducationItem(_ProfileModel):
    school: str
    program: Optional[str] = None
    years: Optional[str] = None


class ExperienceItem(_ProfileModel):
    role: str
    company: str
    years: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)


class ProjectItem(_ProfileModel):
    name: str
    stack: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    name: str
    contact: Contact = Field(default_factory=Contact)
    languages: list[LanguageItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    cv_language: Literal["sl", "en"] = "sl"


# --------------------------------------------------------------------------- #
# LLM stage outputs
# --------------------------------------------------------------------------- #
class JobExtract(BaseModel):
    """Stage A: structured metadata pulled from a single job ad."""
    category: str
    skills: list[str] = Field(default_factory=list)
    language: Literal["sl", "en"]
    work_mode: Literal["remote", "onsite", "unknown"] = "unknown"


class JobScore(BaseModel):
    """Stage B: how well the profile matches the job."""
    score: int = Field(ge=0, le=100)
    fits: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# CV + cover note
# --------------------------------------------------------------------------- #
class CVExperience(BaseModel):
    """An experience entry chosen from the profile, identified by its company."""
    company: str
    bullets: list[str] = Field(default_factory=list)


class CVProject(BaseModel):
    """A project chosen from the profile, identified by its name."""
    name: str
    bullets: list[str] = Field(default_factory=list)


class CVContent(BaseModel):
    """What the model may decide: which entries, in what order, reworded.

    Roles, years, schools and contact details are never taken from the model; they
    are rendered straight from profile.yaml, so they cannot be invented.
    """
    summary: str
    experience: list[CVExperience] = Field(default_factory=list, max_length=3)
    projects: list[CVProject] = Field(default_factory=list, max_length=3)
    skills: list[str] = Field(default_factory=list)


class CoverNote(BaseModel):
    note: str

    @field_validator("note")
    @classmethod
    def _at_most_120_words(cls, v: str) -> str:
        words = len(v.split())
        if words > 120:
            raise ValueError(f"cover note must be 120 words or fewer, got {words}")
        return v
