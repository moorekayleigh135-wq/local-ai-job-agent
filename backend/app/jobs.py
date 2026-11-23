"""Job creation and retrieval endpoints."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.job_extractor import extract_job_details
from app.models import Job, JobScanMatch, SearchPreferences
from app.schemas import JobCreate, JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])

DatabaseSession = Annotated[Session, Depends(get_db)]


@router.post("", response_model=JobRead, status_code=201)
def create_job(payload: JobCreate, db: DatabaseSession) -> Job:
    """Extract details from a job description and save it to MySQL."""

    try:
        details = extract_job_details(payload.description)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not extract job details using the local AI model.",
        ) from exc

    job = Job(
        **details,
        source_url=str(payload.source_url) if payload.source_url else None,
        description=payload.description,
    )

    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("", response_model=list[JobRead])
def list_jobs(
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[JobRead]:
    """List manual saves and scanner jobs that meet the user's match threshold."""

    preferences = db.get(SearchPreferences, 1)
    minimum_match = preferences.min_match_score if preferences else 40
    statement = (
        select(Job, JobScanMatch.match_score)
        .outerjoin(JobScanMatch, JobScanMatch.job_id == Job.id)
        .where(
            or_(
                JobScanMatch.id.is_(None),
                JobScanMatch.match_score >= minimum_match,
            )
        )
        .order_by(
            case((JobScanMatch.match_score.is_(None), 1), else_=0),
            JobScanMatch.match_score.desc(),
            Job.created_at.desc(),
            Job.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return [
        JobRead.model_validate(job, from_attributes=True).model_copy(
            update={"match_score": match_score}
        )
        for job, match_score in db.execute(statement).all()
    ]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: DatabaseSession) -> Job:
    """Retrieve one saved job by its ID."""

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job
