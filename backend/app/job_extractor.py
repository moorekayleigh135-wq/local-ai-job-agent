"""Extract structured job details using the local Ollama model."""
import json

import httpx

from app.config import settings


def extract_job_details(description: str) -> dict:
    """Extract job details without inventing missing information."""
    prompt = f"""
Extract structured information from this job description.
Only use information supported by the description. Do not guess.

Return exactly one JSON object with these keys:
- title: string or null
- company: string or null
- location: string or null
- employment_type: string or null
- job_level: string or null
- work_arrangement: one of "onsite", "hybrid", "remote", or null
- skills: array of strings
- qualifications: array of strings

For job_level, use labels such as "intern", "entry-level",
"associate", "new graduate", "mid-level", or "senior".
Distinguish a senior college student from a senior-level employee.
Use null if the job's professional level is not stated clearly.
For work_arrangement, use only explicit evidence. Do not assume
that a job is onsite just because no remote option is mentioned.

JOB DESCRIPTION:
{description}
"""
    response = httpx.post(
        f"{settings.ollama_url.rstrip('/')}/api/generate",
        json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=180.0,
    )
    response.raise_for_status()

    result = response.json()
    extracted = json.loads(result["response"])
    if not isinstance(extracted, dict):
        raise ValueError("The model did not return a JSON object.")

    def optional_text(key: str, limit: int = 255) -> str | None:
        value = extracted.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:limit]

    skills = extracted.get("skills", [])
    qualifications = extracted.get("qualifications", [])

    if not isinstance(skills, list) or not all(
        isinstance(item, str) for item in skills
    ):
        skills = []

    if not isinstance(qualifications, list) or not all(
        isinstance(item, str) for item in qualifications
    ):
        qualifications = []

    arrangement = optional_text("work_arrangement", 50)
    allowed_arrangements = {"onsite", "hybrid", "remote"}
    if arrangement:
        arrangement = arrangement.lower()
        if arrangement not in allowed_arrangements:
            arrangement = None

    return {
        "title": optional_text("title"),
        "company": optional_text("company"),
        "location": optional_text("location"),
        "employment_type": optional_text("employment_type", 100),
        "job_level": optional_text("job_level", 100),
        "work_arrangement": arrangement,
        "skills": skills[:100],
        "qualifications": qualifications[:100],
    }
