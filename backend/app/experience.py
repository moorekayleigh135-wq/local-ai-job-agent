"""Derive a practical career level from the user's locally stored resume profile."""

from __future__ import annotations

import re
from datetime import datetime


def infer_experience_level(profile: dict | None) -> str | None:
    """Infer a search level from experience and current education in the resume."""
    if not isinstance(profile, dict):
        return None

    current_year = datetime.now().year
    for item in profile.get("education", []):
        if not isinstance(item, dict):
            continue
        evidence = " ".join(
            str(item.get(key) or "") for key in ("credential", "dates")
        ).casefold()
        if re.search(r"\b(in progress|currently enrolled|expected|anticipated|pursuing|candidate for)\b", evidence):
            return "entry_level"
        years_in_dates = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", evidence)]
        if years_in_dates and max(years_in_dates) >= current_year:
            return "entry_level"

    years = profile.get("years_experience")
    if isinstance(years, (int, float)) and not isinstance(years, bool):
        if years < 2:
            return "entry_level"
        if years < 6:
            return "professional"
        return "senior"

    titles = " ".join(
        str(item.get("title") or "")
        for item in profile.get("experience", []) if isinstance(item, dict)
    ).casefold()
    if re.search(r"\b(director|vice president|\bvp\b|chief|executive)\b", titles):
        return "executive"
    if re.search(r"\b(senior|staff|principal|lead)\b", titles):
        return "senior"
    if profile.get("education"):
        return "entry_level"
    return None


def target_experience_level(profile: dict | None, preference: str | None) -> str | None:
    """Use an explicit search choice when present, otherwise resume-derived level."""
    if preference and preference != "any":
        return preference
    return infer_experience_level(profile)
