"""Public job-board feeds exposed by common ATS platforms."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup


def _clean_html(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(BeautifulSoup(value, "html.parser").stripped_strings)[:12_000]


def _salary(value: object) -> dict:
    if isinstance(value, list):
        value = next((
            entry.get("value") for entry in value
            if isinstance(entry, dict)
            and re.search(r"salary|compensation|pay range", str(entry.get("name") or ""), re.I)
        ), None)
    if isinstance(value, str):
        numbers = re.findall(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?", value, re.I)
        parsed = []
        for number, thousands in numbers[:2]:
            try:
                amount = float(number.replace(",", ""))
                parsed.append(amount * 1000 if thousands else amount)
            except ValueError:
                continue
        if not parsed:
            return {}
        period_match = re.search(r"\b(hour|hourly|year|annual|month|weekly)\b", value, re.I)
        currency_match = re.search(r"\b(USD|CAD|EUR|GBP)\b", value, re.I)
        return {
            "salary_min": parsed[0],
            "salary_max": parsed[-1],
            "salary_currency": currency_match.group(1).upper() if currency_match else None,
            "salary_interval": period_match.group(1).casefold() if period_match else "year",
        }
    if not isinstance(value, dict):
        return {}
    amount = value.get("value", value)
    if not isinstance(amount, dict):
        return {}
    try:
        minimum = float(amount.get("minValue", amount.get("min")))
        maximum = float(amount.get("maxValue", amount.get("max")))
    except (TypeError, ValueError):
        return {}
    return {
        "salary_min": minimum,
        "salary_max": maximum,
        "salary_currency": str(value["currency"]).upper()[:8] if value.get("currency") else None,
        "salary_interval": str(amount.get("unitText") or value.get("interval") or "year").casefold()[:24],
    }


def public_ats_postings(client: httpx.Client, source_url: str) -> list[dict] | None:
    """Read listed jobs from public Greenhouse, Lever, and Ashby board endpoints.

    Returns None for unsupported/non-board URLs so the normal HTML scanner can
    handle employer sites. These endpoints expose public listings without
    requiring login or circumventing a verification page.
    """
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    platform = ""
    endpoint = ""
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        platform = "greenhouse"
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{quote(parts[0], safe='')}/jobs"
        params = {"content": "true"}
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        platform = "lever"
        api_host = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        endpoint = f"https://{api_host}/v0/postings/{quote(parts[0], safe='')}"
        params = {"mode": "json"}
    elif host == "jobs.ashbyhq.com":
        platform = "ashby"
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{quote(parts[0], safe='')}"
        params = {"listedOnly": "true"}
    else:
        return None

    try:
        response = client.get(endpoint, params=params, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    if platform == "greenhouse":
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    elif platform == "lever":
        raw_jobs = payload if isinstance(payload, list) else []
    else:
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []

    postings: list[dict] = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        if platform == "greenhouse":
            title = item.get("title")
            location = (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else item.get("location")
            url = item.get("absolute_url")
            description = item.get("content") or ""
            external_id = item.get("id")
            posted = item.get("first_published_at")
            salary = item.get("salaryRange") or item.get("baseSalary") or item.get("metadata")
            employment = item.get("employment_type")
            work_arrangement = None
        elif platform == "lever":
            title = item.get("text")
            categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
            location = categories.get("location") or ", ".join(categories.get("allLocations", []) if isinstance(categories.get("allLocations"), list) else [])
            url = item.get("hostedUrl")
            description = item.get("descriptionPlain") or item.get("description") or ""
            external_id = item.get("id")
            posted = item.get("createdAt") or item.get("publishedAt")
            salary = item.get("salaryRange")
            employment = categories.get("commitment")
            work_arrangement = item.get("workplaceType")
        else:
            title = item.get("title")
            location = item.get("location")
            url = item.get("jobUrl") or item.get("applyUrl")
            description = item.get("descriptionHtml") or item.get("description") or ""
            external_id = item.get("id")
            posted = item.get("publishedAt")
            salary = item.get("compensation") or item.get("salaryRange")
            employment = item.get("employmentType")
            work_arrangement = item.get("workplaceType")

        if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.startswith("https://"):
            continue
        posting_host = (urlsplit(url).hostname or "").casefold()
        allowed_job_hosts = {
            "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
            "lever": {"jobs.lever.co", "jobs.eu.lever.co"},
            "ashby": {"jobs.ashbyhq.com"},
        }[platform]
        if posting_host not in allowed_job_hosts or urlsplit(url).username or urlsplit(url).password:
            continue
        if isinstance(location, dict):
            location = location.get("name")
        if isinstance(employment, list):
            employment = ", ".join(str(value) for value in employment)
        if not isinstance(employment, str):
            employment = None
        if not isinstance(work_arrangement, str):
            work_arrangement = None
        salary_data = _salary(salary)
        postings.append({
            "title": title.strip()[:255],
            "url": url[:2048],
            "description": _clean_html(description),
            "location": str(location)[:255] if location else None,
            "external_id": f"{host}:{external_id}"[:160] if external_id else None,
            "datePosted": posted,
            "employmentType": employment,
            "workplaceType": work_arrangement,
            **salary_data,
        })
    return postings
