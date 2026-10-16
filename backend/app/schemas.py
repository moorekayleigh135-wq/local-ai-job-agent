"""Pydantic schemas for job API requests and responses."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


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
