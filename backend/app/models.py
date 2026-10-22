"""SQLAlchemy models for job postings."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.security import EncryptedJSON


class Job(Base):
    """A job posting and its AI-extracted details."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    job_level: Mapped[str | None] = mapped_column(String(100), nullable=True)
    work_arrangement: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    skills: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    qualifications: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AuthAccount(Base):
    """The one local account that owns this single-user installation."""

    __tablename__ = "auth_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AuthSession(Base):
    """Hashed bearer tokens for browser sessions; raw tokens stay in cookies."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("auth_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class CareerSite(Base):
    """A public company careers page the user explicitly chose to scan."""

    __tablename__ = "career_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class DiscoveredCareerSite(Base):
    """Public career-page suggestions awaiting the user's monitoring choice."""

    __tablename__ = "discovered_career_sites"
    __table_args__ = (UniqueConstraint("url", name="uq_discovered_career_site_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class CompanyDiscoveryState(Base):
    """Last automatic company discovery run for this single-user installation."""

    __tablename__ = "company_discovery_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(400))


class SearchPreferences(Base):
    """The user's single saved job-search profile."""

    __tablename__ = "search_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    role_keywords: Mapped[list[str]] = mapped_column(EncryptedJSON(), default=list, nullable=False)
    locations: Mapped[list[str]] = mapped_column(EncryptedJSON(), default=list, nullable=False)
    skills: Mapped[list[str]] = mapped_column(EncryptedJSON(), default=list, nullable=False)
    work_arrangements: Mapped[list[str]] = mapped_column(EncryptedJSON(), default=list, nullable=False)
    min_match_score: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationSettings(Base):
    """Local notification and automatic scan controls for this installation."""

    __tablename__ = "notification_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_notifications_per_day: Mapped[int] = mapped_column(Integer, default=10, nullable=False)


class CandidateProfile(Base):
    """Structured, user-reviewed career profile extracted from a resume."""

    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_data: Mapped[dict] = mapped_column(EncryptedJSON(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JobScanMatch(Base):
    """Latest preference match calculated for a discovered job."""

    __tablename__ = "job_scan_matches"
    __table_args__ = (UniqueConstraint("job_id", name="uq_job_scan_match_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    match_score: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_terms: Mapped[list[str]] = mapped_column(EncryptedJSON(), default=list, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class JobNotification(Base):
    """A locally stored alert for a newly recommended job."""

    __tablename__ = "job_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_score: Mapped[int] = mapped_column(Integer, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class BrowserPushSubscription(Base):
    """Encrypted browser push endpoint and its per-device secrets."""

    __tablename__ = "browser_push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    subscription_data: Mapped[dict] = mapped_column(EncryptedJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class PushServerKey(Base):
    """Locally generated VAPID key; the private half is encrypted at rest."""

    __tablename__ = "push_server_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_key: Mapped[str] = mapped_column(String(128), nullable=False)
    private_key: Mapped[dict] = mapped_column(EncryptedJSON(), nullable=False)
