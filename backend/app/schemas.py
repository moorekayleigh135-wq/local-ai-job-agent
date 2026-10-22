"""Pydantic schemas for job API requests and responses."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ExperienceItem(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    dates: str | None = Field(default=None, max_length=100)
    highlights: list[str] = Field(default_factory=list, max_length=8)


class EducationItem(BaseModel):
    institution: str | None = Field(default=None, max_length=200)
    credential: str | None = Field(default=None, max_length=200)
    dates: str | None = Field(default=None, max_length=100)


class CandidateProfileData(BaseModel):
    headline: str | None = Field(default=None, max_length=200)
    professional_summary: str | None = Field(default=None, max_length=3000)
    years_experience: float | None = Field(default=None, ge=0, le=80)
    skills: list[str] = Field(default_factory=list, max_length=100)
    role_keywords: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    work_arrangements: list[str] = Field(default_factory=list, max_length=3)
    experience: list[ExperienceItem] = Field(default_factory=list, max_length=30)
    education: list[EducationItem] = Field(default_factory=list, max_length=20)
    certifications: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("work_arrangements")
    @classmethod
    def validate_work_arrangements(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip().casefold() for value in values if value.strip()]
        if any(value not in {"remote", "hybrid", "onsite"} for value in cleaned):
            raise ValueError("Work arrangements must be remote, hybrid, or onsite.")
        return list(dict.fromkeys(cleaned))


class CandidateProfileRead(BaseModel):
    filename: str
    profile: CandidateProfileData
    updated_at: datetime


class JobCreate(BaseModel):
    description: str = Field(min_length=30, max_length=50000)
    source_url: HttpUrl | None = None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    company: str | None
    location: str | None
    employment_type: str | None
    job_level: str | None
    work_arrangement: str | None
    source_url: str | None
    description: str
    skills: list[str]
    qualifications: list[str]
    created_at: datetime


class CareerSiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: HttpUrl = Field(max_length=512)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Company name cannot be blank.")
        return value


class CareerSiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    last_scanned_at: datetime | None
    created_at: datetime


class SearchPreferencesUpdate(BaseModel):
    role_keywords: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=30)
    work_arrangements: list[str] = Field(default_factory=list, max_length=3)
    min_match_score: int = Field(default=40, ge=0, le=100)

    def model_post_init(self, __context) -> None:
        self.role_keywords = _clean_terms(self.role_keywords)
        self.locations = _clean_terms(self.locations)
        self.skills = _clean_terms(self.skills)
        self.work_arrangements = [
            value.casefold() for value in _clean_terms(self.work_arrangements)
        ]
        if any(item not in {"remote", "hybrid", "onsite"} for item in self.work_arrangements):
            raise ValueError("Work arrangements must be remote, hybrid, or onsite.")


class SearchPreferencesRead(SearchPreferencesUpdate):
    model_config = ConfigDict(from_attributes=True)

    updated_at: datetime | None = None


class ScannedJobRead(BaseModel):
    job: JobRead
    match_score: int
    matched_terms: list[str]
    scanned_at: datetime


class ScanSummary(BaseModel):
    company: str
    scanned_count: int
    matched_count: int
    new_count: int
    updated_count: int
    skipped_count: int
    warnings: list[str] = Field(default_factory=list)


def _clean_terms(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(value.split()).strip()[:100]
        key = term.casefold()
        if term and key not in seen:
            cleaned.append(term)
            seen.add(key)
    return cleaned
