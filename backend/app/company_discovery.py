"""Discover public employer career pages from user-approved search terms."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import httpx

from app.config import settings

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
RESULT_LIMIT = 30
EXCLUDED_DOMAINS = {
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "handshake.com",
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
        q = f'("careers" OR "jobs") ({group_clause})'
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

    if any(matches_domain(domain) for domain in (
        "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com"
    )):
        segments = segments[:1]
    elif matches_domain("myworkdayjobs.com"):
        segments = segments[:2]
    for index, segment in enumerate(segments):
        if segment.casefold() in CAREER_PATH_HINTS and not matches_domain("myworkdayjobs.com"):
            segments = segments[:index + 1]
            break
    path = "/" + "/".join(segments) if segments else ""
    return parsed._replace(path=path, query="", fragment="").geturl().rstrip("/")


def discover_company_sites(role_keywords: list[str], locations: list[str]) -> list[dict[str, str]]:
    """Search the public web using only the explicitly selected role/location terms."""
    if not settings.brave_search_api_key:
        raise CompanyDiscoveryError(
            "Company discovery is not configured. Add BRAVE_SEARCH_API_KEY to your .env file."
        )

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": settings.brave_search_api_key,
    }
    found: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), headers=headers) as client:
            for query in _query_groups(role_keywords, locations):
                response = client.get(
                    BRAVE_SEARCH_URL,
                    params={
                        "q": query,
                        "count": 20,
                        "search_lang": "en",
                        "safesearch": "moderate",
                    },
                )
                if response.status_code in {401, 403}:
                    raise CompanyDiscoveryError(
                        "Brave Search rejected its API key. Check BRAVE_SEARCH_API_KEY in .env."
                    )
                if response.status_code == 429:
                    raise CompanyDiscoveryError(
                        "Brave Search rate limit reached. Discovery will retry on its next scheduled run."
                    )
                response.raise_for_status()
                payload = response.json()
                results = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
                if not isinstance(results, list):
                    continue
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    title = item.get("title")
                    description = item.get("description", "")
                    if not _is_candidate_url(url) or not isinstance(title, str):
                        continue
                    if not isinstance(description, str):
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
    except httpx.TimeoutException as exc:
        raise CompanyDiscoveryError("Brave Search timed out. Discovery will retry later.") from exc
    except httpx.HTTPError as exc:
        raise CompanyDiscoveryError("Brave Search could not be reached. Check the server's internet connection.") from exc
    except (ValueError, TypeError) as exc:
        raise CompanyDiscoveryError("Brave Search returned an unreadable response.") from exc
    return list(found.values())
