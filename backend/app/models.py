"""SQLAlchemy models for the local AI job-search agent."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Company(Base):
    """Company that publishes job openings."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    website_url: Mapped[str | None] = mapped_column(String(2048))
    careers_url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    jobs: Mapped[list["JobPosting"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )


class JobPosting(Base):
    """Job collected from a company career page or job source."""

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_id",
            name="uq_job_source_external_id",
        ),
        Index("ix_job_postings_first_seen_at", "first_seen_at"),
        Index("ix_job_postings_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(512), index=True)
    location: Mapped[str | None] = mapped_column(String(512))
    employment_type: Mapped[str | None] = mapped_column(String(128))
    workplace_type: Mapped[str | None] = mapped_column(String(64))

    description: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))

    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )
    content_hash: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    matches: Mapped[list["JobMatch"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    application: Mapped["JobApplication | None"] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        uselist=False,
    )


class JobMatch(Base):
    """AI-generated compatibility score and explanation for a job."""

    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "profile_version",
            name="uq_job_match_profile_version",
        ),
        Index("ix_job_matches_score", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        index=True,
    )
    profile_version: Mapped[str] = mapped_column(String(64))
    score: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    explanation: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[str] | None] = mapped_column(JSON)
    gaps: Mapped[list[str] | None] = mapped_column(JSON)
    model_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

    job: Mapped[JobPosting] = relationship(back_populates="matches")


class JobApplication(Base):
    """User-managed tracking information for a job."""

    __tablename__ = "job_applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="saved",
        index=True,
    )
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    job: Mapped[JobPosting] = relationship(back_populates="application")


class SearchRun(Base):
    """Record of a source-import or job-search run."""

    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    jobs_found: Mapped[int] = mapped_column(default=0)
    jobs_created: Mapped[int] = mapped_column(default=0)
    jobs_updated: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
