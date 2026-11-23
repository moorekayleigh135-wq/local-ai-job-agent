"""Discover public employer career pages from user-approved search terms."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import httpx
from app.config import settings

SEARXNG_SEARCH_PATH = "/search"
RESULT_LIMIT = 30
EXCLUDED_DOMAINS = {
    "simplyhired.com",
    "monster.com",
    "careerbuilder.com",
    "talent.com",
    "jooble.org",
    "adzuna.com",
    "builtin.com",
    "wellfound.com",
}
CAREER_HINTS = (
    "career", "jobs", "job", "workday", "greenhouse", "lever", "ashby",
    "smartrecruiters", "workable", "icims", "employment", "talent",
)
CAREER_PATH_HINTS = {"careers", "career", "jobs", "job", "openings", "positions"}


class CompanyDiscoveryError(ValueError):
    """The configured search provider could not discover employer sites."""


def _query_groups(role_keywords: list[str], locations: list[str]) -> list[str]:
    roles = [" ".join(term.split())[:60] for term in role_keywords if term.strip()][:8]
    places = [" ".join(term.split())[:60] for term in locations if term.strip()][:4]
    if not roles:
        raise CompanyDiscoveryError("Add at least one target role before discovering companies.")

    role_groups = [roles[index:index + 4] for index in range(0, len(roles), 4)][:2]
    phrase = lambda term: '"' + term.replace('"', '') + '"'
    location_clause = " OR ".join(phrase(term) for term in places)
    # Keep each provider query useful and bounded. Split very broad role lists
    # into at most two requests, while retaining all location terms.
    queries: list[str] = []
    for group in role_groups:
        group_clause = " OR ".join(phrase(term) for term in group)
        # Search results are discovery hints only. The scanner verifies each
        # individual posting directly at its employer/board URL before saving
        # or alerting; snippets and AI summaries are never treated as evidence.
        q = f'("careers" OR "jobs" OR "job openings" OR "job posting") ({group_clause})'
        if location_clause:
            q += f" ({location_clause})"
        queries.append(q)
    return queries


def _is_candidate_url(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 512:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in {None, 443}:
            return False
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        if any(host == domain or host.endswith("." + domain) for domain in EXCLUDED_DOMAINS):
            return False
        return True
    except ValueError:
        return False


def _company_name(title: str, url: str) -> str:
    name = re.split(r"\s+[|·–—-]\s+", title.strip(), maxsplit=1)[0]
    name = re.sub(r"\b(careers?|jobs?|open positions|job openings)\b", "", name, flags=re.I)
    name = " ".join(name.split()).strip(" -|·–—")
    if name:
        return name[:255]
    return (urlsplit(url).hostname or "Company careers site")[:255]


def _career_page_url(url: str) -> str:
    """Prefer a career listing root over an individual job result URL."""
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    host = (parsed.hostname or "").casefold()
    def matches_domain(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if matches_domain("ibm.com"):
        if host == "ibm.com" or host == "www.ibm.com":
            return "https://www.ibm.com/careers/search"
        return "https://careers.ibm.com/en_US/careers"
    if any(matches_domain(domain) for domain in (
        "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com"
    )):
        segments = segments[:1]
    elif matches_domain("myworkdayjobs.com"):
        segments = segments[:2]
    elif matches_domain("linkedin.com") or matches_domain("indeed.com"):
        segments = ["jobs"]
    elif matches_domain("handshake.com"):
        segments = ["jobs"]
    elif matches_domain("glassdoor.com"):
        segments = ["Job"]
    for index, segment in enumerate(segments):
        if segment.casefold() in CAREER_PATH_HINTS and not matches_domain("myworkdayjobs.com"):
            segments = segments[:index + 1]
            break
    path = "/" + "/".join(segments) if segments else ""
    return parsed._replace(path=path, query="", fragment="").geturl().rstrip("/")


def discover_company_sites(role_keywords: list[str], locations: list[str]) -> list[dict[str, str]]:
    """Search through the local SearXNG instance using saved role/location terms."""
    search_url = f"{settings.searxng_url.rstrip('/')}{SEARXNG_SEARCH_PATH}"
    found: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Accept": "application/json"},
        ) as client:
            for query in _query_groups(role_keywords, locations):
                response = client.get(
                    search_url,
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "general",
                        "language": "en",
                        "time_range": "month",
                    },
                )
                if response.status_code == 429:
                    raise CompanyDiscoveryError("The free search service is temporarily rate limited. Try again later.")
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results", []) if isinstance(payload, dict) else []
                if not isinstance(results, list):
                    raise CompanyDiscoveryError("The free search service returned an unreadable result list.")
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    title = item.get("title")
                    description = item.get("content", "")
                    if not _is_candidate_url(url) or not isinstance(title, str):
                        continue
                    if not isinstance(description, str):
                        description = ""
                    # Never present search snippets (including AI-generated
                    # summaries) as verified job details.
                    description = ""
                    signals = f"{title} {url} {description}".casefold()
                    if not any(hint in signals for hint in CAREER_HINTS):
                        continue
                    normalized_url = _career_page_url(url)
                    found.setdefault(normalized_url, {
                        "name": _company_name(title, normalized_url),
                        "url": normalized_url[:512],
                        "description": " ".join(description.split())[:1000],
                    })
                    if len(found) >= RESULT_LIMIT:
                        return list(found.values())
    except CompanyDiscoveryError:
        raise
    except httpx.HTTPStatusError as exc:
        raise CompanyDiscoveryError(
            f"The free search service returned HTTP {exc.response.status_code}. Discovery will retry later."
        ) from exc
    except httpx.TimeoutException as exc:
        raise CompanyDiscoveryError("The free search service timed out. Discovery will retry later.") from exc
    except httpx.HTTPError as exc:
        raise CompanyDiscoveryError(
            "The local search service could not be reached. Check that its Compose service is running."
        ) from exc
    except (ValueError, TypeError) as exc:
        raise CompanyDiscoveryError("The free search service returned an unreadable response.") from exc
    return list(found.values())
