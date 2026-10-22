"""Company career-page and search-preference endpoints."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.career_scanner import ScanError, scan_career_site
from app.database import get_db
from app.models import CareerSite, Job, JobScanMatch, SearchPreferences
from app.schemas import (
    CareerSiteCreate,
    CareerSiteRead,
    ScanSummary,
    ScannedJobRead,
    SearchPreferencesRead,
    SearchPreferencesUpdate,
)

router = APIRouter(prefix="/scanner", tags=["company scanner"])
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("/companies", response_model=list[CareerSiteRead])
def list_companies(db: DatabaseSession) -> list[CareerSite]:
    return list(db.scalars(select(CareerSite).order_by(CareerSite.name)).all())


@router.post("/companies", response_model=CareerSiteRead, status_code=status.HTTP_201_CREATED)
def add_company(payload: CareerSiteCreate, db: DatabaseSession) -> CareerSite:
    site = CareerSite(name=payload.name.strip(), url=str(payload.url))
    db.add(site)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That careers URL is already saved.") from exc
    db.refresh(site)
    return site


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_company(company_id: int, db: DatabaseSession) -> Response:
    site = db.get(CareerSite, company_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    db.delete(site)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=SearchPreferencesRead)
def get_preferences(db: DatabaseSession) -> SearchPreferencesRead:
    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        return SearchPreferencesRead()
    return SearchPreferencesRead.model_validate(preferences, from_attributes=True)


@router.put("/preferences", response_model=SearchPreferencesRead)
def save_preferences(
    payload: SearchPreferencesUpdate,
    db: DatabaseSession,
) -> SearchPreferencesRead:
    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        preferences = SearchPreferences(id=1)
        db.add(preferences)
    preferences.role_keywords = payload.role_keywords
    preferences.locations = payload.locations
    preferences.skills = payload.skills
    preferences.work_arrangements = payload.work_arrangements
    preferences.min_match_score = payload.min_match_score
    db.commit()
    db.refresh(preferences)
    return SearchPreferencesRead.model_validate(preferences, from_attributes=True)


@router.get("/matches", response_model=list[ScannedJobRead])
def list_matches(
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ScannedJobRead]:
    rows = db.execute(
        select(JobScanMatch, Job)
        .join(Job, Job.id == JobScanMatch.job_id)
        .order_by(JobScanMatch.match_score.desc(), JobScanMatch.scanned_at.desc())
        .limit(limit)
    ).all()
    return [
        ScannedJobRead(
            job=job,
            match_score=match.match_score,
            matched_terms=match.matched_terms,
            scanned_at=match.scanned_at,
        )
        for match, job in rows
    ]


@router.post("/companies/{company_id}/scan", response_model=ScanSummary)
def scan_company(company_id: int, db: DatabaseSession) -> ScanSummary:
    site = db.get(CareerSite, company_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    preferences = db.get(SearchPreferences, 1)
    if preferences is None or not any(
        [preferences.role_keywords, preferences.locations, preferences.skills, preferences.work_arrangements]
    ):
        raise HTTPException(status_code=400, detail="Save at least one search preference before scanning.")
    preference_data = {
        "role_keywords": preferences.role_keywords,
        "locations": preferences.locations,
        "skills": preferences.skills,
        "work_arrangements": preferences.work_arrangements,
        "min_match_score": preferences.min_match_score,
    }
    try:
        postings, skipped_count, warnings = scan_career_site(site.url, preference_data)
    except ScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_count = 0
    updated_count = 0
    for posting in postings:
        job = db.scalar(select(Job).where(Job.source_url == posting.url).limit(1))
        if job is None:
            job = Job(
                title=posting.title,
                company=site.name,
                source_url=posting.url,
                description=posting.description,
                skills=[],
                qualifications=[],
            )
            db.add(job)
            db.flush()
            new_count += 1
        else:
            job.title = posting.title
            job.company = site.name
            job.description = posting.description
            updated_count += 1

        match = db.scalar(select(JobScanMatch).where(JobScanMatch.job_id == job.id))
        if match is None:
            db.add(
                JobScanMatch(
                    job_id=job.id,
                    match_score=posting.score,
                    matched_terms=posting.matched_terms,
                    scanned_at=now,
                )
            )
        else:
            match.match_score = posting.score
            match.matched_terms = posting.matched_terms
            match.scanned_at = now

    site.last_scanned_at = now
    db.commit()
    return ScanSummary(
        company=site.name,
        scanned_count=len(postings) + skipped_count,
        matched_count=len(postings),
        new_count=new_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        warnings=warnings,
    )
