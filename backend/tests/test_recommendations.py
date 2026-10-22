from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import scans
from app.config import settings
from app.database import Base
from app.career_scanner import ScannedPosting
from app.models import CareerSite, JobNotification, JobScanMatch, SearchPreferences


def test_scan_creates_alert_when_a_job_meets_the_score_threshold(monkeypatch) -> None:
    monkeypatch.setattr(settings, "profile_encryption_key", "ef" * 32)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    site = CareerSite(id=1, name="Example Co", url="https://example.com/careers")
    preferences = SearchPreferences(
        id=1,
        role_keywords=["Designer"],
        locations=["Boston"],
        skills=["Figma"],
        work_arrangements=[],
        min_match_score=60,
    )
    with Session(engine) as db:
        db.add_all([site, preferences])
        db.commit()
        monkeypatch.setattr(
            scans,
            "scan_career_site",
            lambda *_: ([ScannedPosting("https://example.com/jobs/1", "Product Designer", "Description", 82, ["Designer", "Figma"])], 0, []),
        )
        result = scans.scan_company(1, db)
        assert result.matched_count == 1
        assert result.notified_count == 1
        assert result.below_threshold_count == 0

        match = db.scalar(select(JobScanMatch))
        notice = db.scalar(select(JobNotification))
        assert match.match_score == 82
        assert notice.match_score == 82

        # Rescanning a job that already qualified does not create duplicate alerts.
        result = scans.scan_company(1, db)
        assert result.notified_count == 0
        assert len(db.scalars(select(JobNotification)).all()) == 1
    engine.dispose()


def test_recommendation_tiers_are_based_on_weighted_match_percentage() -> None:
    assert scans.recommendation_for_score(80) == "Strong match"
    assert scans.recommendation_for_score(60) == "Good match"
    assert scans.recommendation_for_score(59) == "Potential match"
    assert "Prioritize" in scans.recommendation_tip_for_score(85)
