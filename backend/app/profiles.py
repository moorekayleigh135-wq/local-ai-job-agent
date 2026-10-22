"""Private resume upload and candidate profile endpoints."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CandidateProfile, SearchPreferences
from app.resume_profiles import (
    MAX_RESUME_BYTES,
    ProfileBuildError,
    ResumeError,
    extract_candidate_profile,
    extract_resume_text,
    safe_filename,
)
from app.schemas import CandidateProfileRead, SearchPreferencesRead

router = APIRouter(prefix="/profile", tags=["candidate profile"])


def _profile_response(record: CandidateProfile) -> dict:
    stored = record.profile_data
    if isinstance(stored, dict) and "profile" in stored:
        profile = stored["profile"]
        filename = stored.get("filename", "Encrypted profile")
    else:  # Compatibility for a row read before the startup migration runs.
        profile = stored
        filename = record.filename
    return {
        "filename": filename,
        "profile": profile,
        "updated_at": record.updated_at,
    }


@router.get("", response_model=CandidateProfileRead | None)
def get_profile(db: Session = Depends(get_db)) -> CandidateProfileRead | None:
    record = db.get(CandidateProfile, 1)
    return _profile_response(record) if record else None


@router.post("/resume", response_model=CandidateProfileRead, status_code=status.HTTP_200_OK)
def upload_resume(
    resume: UploadFile = File(...), db: Session = Depends(get_db)
) -> CandidateProfileRead:
    """Extract and save a structured profile; the uploaded file is never persisted."""
    filename = safe_filename(resume.filename)
    content = resume.file.read(MAX_RESUME_BYTES + 1)
    try:
        resume_text = extract_resume_text(filename, content)
    except ResumeError as exc:
        code = 413 if len(content) > MAX_RESUME_BYTES else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    try:
        profile = extract_candidate_profile(resume_text)
    except ProfileBuildError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    record = db.get(CandidateProfile, 1)
    if record is None:
        record = CandidateProfile(
            id=1,
            filename="Encrypted profile",
            profile_data={"filename": filename, "profile": profile.model_dump(mode="json")},
        )
        db.add(record)
    else:
        record.filename = "Encrypted profile"
        record.profile_data = {"filename": filename, "profile": profile.model_dump(mode="json")}
    db.commit()
    db.refresh(record)
    return _profile_response(record)


@router.post("/apply-to-search", response_model=SearchPreferencesRead)
def apply_profile_to_search(db: Session = Depends(get_db)) -> SearchPreferences:
    """Add resume-derived terms to the scanner profile after the user requests it."""
    profile = db.get(CandidateProfile, 1)
    if profile is None:
        raise HTTPException(status_code=404, detail="Upload a resume to build your profile first.")

    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        preferences = SearchPreferences(id=1)
        db.add(preferences)

    stored = profile.profile_data
    additions = stored.get("profile", stored) if isinstance(stored, dict) else {}
    for field, maximum in (
        ("role_keywords", 20),
        ("locations", 20),
        ("skills", 30),
        ("work_arrangements", 3),
    ):
        existing = list(getattr(preferences, field) or [])
        seen = {value.casefold() for value in existing}
        for value in additions.get(field, []):
            if isinstance(value, str) and value.strip() and value.casefold() not in seen:
                existing.append(value.strip())
                seen.add(value.casefold())
        setattr(preferences, field, existing[:maximum])

    db.commit()
    db.refresh(preferences)
    return preferences
