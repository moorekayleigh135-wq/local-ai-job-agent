"""Job creation and retrieval endpoints."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.job_extractor import extract_job_details
from app.models import Job
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
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Job]:
    """List saved jobs, newest first."""

    statement = (
        select(Job)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(statement).all())


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: DatabaseSession) -> Job:
    """Retrieve one saved job by its ID."""

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job
