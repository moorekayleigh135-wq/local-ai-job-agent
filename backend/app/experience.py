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

    years = candidate_years_experience(profile)
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


def candidate_years_experience(profile: dict | None) -> float | None:
    """Count internships and reasonable academic progress as early-career experience."""
    if not isinstance(profile, dict):
        return None
    stated = profile.get("years_experience")
    base = float(stated) if isinstance(stated, (int, float)) and not isinstance(stated, bool) else 0.0
    internship_credit = 0.0
    for item in profile.get("experience", []):
        if not isinstance(item, dict):
            continue
        title_and_kind = " ".join(str(item.get(key) or "") for key in ("title", "kind")).casefold()
        if not re.search(r"intern|co[- ]?op", title_and_kind):
            continue
        dates = str(item.get("dates") or "")
        month_years = re.findall(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(20\d{2})\b",
            dates,
            re.I,
        )
        year_values = [int(value) for value in re.findall(r"\b20\d{2}\b", dates)]
        if len(month_years) >= 2:
            first = datetime(int(month_years[0][1]), 1, 1).replace(
                month=datetime.strptime(month_years[0][0][:3].title(), "%b").month
            )
            last = datetime(int(month_years[-1][1]), 1, 1).replace(
                month=datetime.strptime(month_years[-1][0][:3].title(), "%b").month
            )
            months = max(1, (last.year - first.year) * 12 + last.month - first.month + 1)
            internship_credit += min(1.0, months / 12)
        elif len(year_values) >= 2:
            internship_credit += min(1.0, max(0.5, year_values[-1] - year_values[0]))
        else:
            internship_credit += 0.5

    internship_credit = min(2.0, internship_credit)
    academic_credit = 0.0
    year_levels = " ".join(
        " ".join(str(item.get(key) or "") for key in ("year_level", "credential", "dates"))
        for item in profile.get("education", []) if isinstance(item, dict)
    ).casefold()
    if re.search(r"\bsenior\b|fourth[- ]year|4th[- ]year", year_levels):
        academic_credit = 2.0
    elif re.search(r"\bjunior\b|third[- ]year|3rd[- ]year", year_levels):
        academic_credit = 1.0
    elif re.search(r"\bsophomore\b|second[- ]year|2nd[- ]year", year_levels):
        academic_credit = 0.5
    elif re.search(r"in progress|currently enrolled|expected|pursuing|candidate for", year_levels):
        academic_credit = 0.5
    valid_stated = isinstance(stated, (int, float)) and not isinstance(stated, bool)
    if not valid_stated and internship_credit == 0 and academic_credit == 0:
        return None
    # Do not double-count a currently enrolled program when the resume already
    # reports equivalent experience. Keep school credit bounded at two years.
    return round(max(base, min(20.0, base + internship_credit + academic_credit)), 1)


def target_experience_level(profile: dict | None, preference: str | None) -> str | None:
    """Use an explicit search choice when present, otherwise resume-derived level."""
    if preference and preference != "any":
        return preference
    return infer_experience_level(profile)
