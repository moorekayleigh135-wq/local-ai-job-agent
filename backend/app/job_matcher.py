"""Private, local-model fit scoring for verified job postings."""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from app.config import settings


class PostingFit(BaseModel):
    index: int = Field(ge=0)
    role_fit: int = Field(ge=0, le=100)
    skills_fit: int = Field(ge=0, le=100)
    experience_fit: int = Field(ge=0, le=100)
    preference_fit: int = Field(ge=0, le=100)
    matched_evidence: list[str] = Field(default_factory=list, max_length=6)


class PostingFitBatch(BaseModel):
    results: list[PostingFit] = Field(default_factory=list)


def score_postings_locally(
    postings: list[object], preferences: dict
) -> dict[int, PostingFit] | None:
    """Compare verified postings with the saved profile without external APIs."""
    if not postings:
        return {}
    candidate = preferences.get("candidate_profile") or {}
    if not isinstance(candidate, dict):
        candidate = {}
    def list_value(source: dict, key: str, limit: int) -> list:
        value = source.get(key)
        return value[:limit] if isinstance(value, list) else []

    compact_profile = {
        "headline": candidate.get("headline"),
        "summary": str(candidate.get("professional_summary") or "")[:1800],
        "years_experience": candidate.get("years_experience"),
        "skills": list_value(candidate, "skills", 30),
        "role_keywords": list_value(candidate, "role_keywords", 15),
        "experience": [
            {
                "title": item.get("title"),
                "highlights": [
                    str(value)[:200] for value in list_value(item, "highlights", 3)
                ],
            }
            for item in list_value(candidate, "experience", 8)
            if isinstance(item, dict)
        ],
    }
    jobs = [
        {
            "index": index,
            "title": posting.title,
            "level": posting.job_level,
            "location": posting.location,
            "employment_type": posting.employment_type,
            "source_id": posting.external_id,
            "description": posting.description[:3200],
        }
        for index, posting in enumerate(postings)
    ]
    prompt = f"""Assess fit for these already source-verified job postings. Use only evidence in the candidate profile, search preferences, and posting text. Do not invent experience or qualifications. Treat listed target roles as alternatives. A role should score highly only when its work is meaningfully related to the candidate's target roles or demonstrated experience. Compare seniority and years requirements; do not reject a role solely for one missing preferred skill.

Return one result for each input index. Give four 0-100 component scores: role_fit, skills_fit, experience_fit, preference_fit. Use 50 for a component that the posting does not disclose. matched_evidence must be short phrases explicitly supported by both sides. Do not write prose outside the JSON.

Weights: role fit 40%, skills fit 30%, experience fit 20%, and location/work-style preference fit 10%. The application calculates the final percentage from these components.

CANDIDATE PROFILE (stored locally):
{json.dumps(compact_profile, ensure_ascii=False)}

SEARCH PREFERENCES:
{json.dumps({key: preferences.get(key) for key in ('role_keywords', 'skills', 'locations', 'work_arrangements', 'experience_level', 'effective_experience_level')}, ensure_ascii=False)}

VERIFIED SOURCE POSTINGS:
{json.dumps(jobs, ensure_ascii=False)}"""
    try:
        response = httpx.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "format": PostingFitBatch.model_json_schema(),
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 1200},
            },
            timeout=httpx.Timeout(120.0, connect=5.0),
        )
        response.raise_for_status()
        payload = response.json()
        parsed = PostingFitBatch.model_validate_json(payload["response"])
        return {result.index: result for result in parsed.results if result.index < len(postings)}
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
