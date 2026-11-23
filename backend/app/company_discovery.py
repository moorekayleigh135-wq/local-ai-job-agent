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
    "shifts.com",
    "tealhq.com",
    # Discovery suggests employer career sources, not third-party boards,
    # recruiting agencies, salary pages, or scraped job-search destinations.
    "indeed.com",
    "linkedin.com",
    "handshake.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "zippia.com",
    "levels.fyi",
    "roberthalf.com",
    "randstadusa.com",
    "zrgpartners.com",
}
CAREER_HINTS = (
    "career", "jobs", "job", "workday", "greenhouse", "lever", "ashby",
    "smartrecruiters", "workable", "icims", "employment", "talent",
)
CAREER_PATH_HINTS = {"careers", "career", "jobs", "job", "openings", "positions", "search-jobs", "search_jobs"}
NON_CAREER_PATHS = {
    "about", "team", "people", "salary", "salaries", "reviews", "review",
    "blog", "resources", "career-advice", "career-center", "who-we-are",
}
NOISE_TITLE_PATTERN = re.compile(
    r"\b(meet (?:the )?team|our team|about us|salary|salaries|company reviews?|"
    r"career advice|career resources|job search tips|jobs near you|"
    r"hiring statistics)\b",
    re.I,
)
ROLE_TOKEN_ALIASES = {
    "analyst": "analytics",
    "analysis": "analytics",
    "engineer": "engineering",
    "strategist": "strategy",
}
ROLE_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "the", "to", "job", "jobs", "career", "careers"}
SENIOR_TITLE_PATTERN = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|vp|vice president|chief|head of|manager)\b",
    re.I,
)


class CompanyDiscoveryError(ValueError):
    """The configured search provider could not discover employer sites."""


def _query_groups(
    role_keywords: list[str], locations: list[str], experience_level: str | None = None
) -> list[str]:
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
        level_terms = {
            "internship": '("internship" OR "intern")',
            "entry_level": '("entry-level" OR "early career" OR "junior")',
        }.get(experience_level)
        if level_terms:
            q += f" {level_terms}"
        if location_clause:
            q += f" ({location_clause})"
        queries.append(q)
    return queries


def is_excluded_company_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    except ValueError:
        return True
    if any(host == domain or host.endswith("." + domain) for domain in EXCLUDED_DOMAINS):
        return True
    if any(segment in NON_CAREER_PATHS for segment in segments):
        return True
    return not _is_career_destination(url)


def _is_career_destination(url: str) -> bool:
    """Keep employer career roots and ATS pages; reject generic company pages."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    if host.startswith(("careers.", "career.", "jobs.", "job.")):
        return True
    ats_domains = (
        "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
        "myworkdayjobs.com", "workday.com", "workable.com", "icims.com",
        "jobvite.com", "successfactors.com",
    )
    if any(host == domain or host.endswith("." + domain) for domain in ats_domains):
        return True
    return any(segment in CAREER_PATH_HINTS for segment in segments)


def _role_tokens(value: str) -> set[str]:
    return {
        ROLE_TOKEN_ALIASES.get(token, token)
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in ROLE_STOP_WORDS and (len(token) >= 3 or token in {"it", "qa"})
    }


def _role_relevant(text: str, role_keywords: list[str]) -> bool:
    """Require a whole target phrase (or the complete multiword role) in the result."""
    result_tokens = _role_tokens(text)
    for role in role_keywords:
        role_tokens = _role_tokens(role)
        if not role_tokens:
            continue
        if len(role_tokens) == 1 and role_tokens <= result_tokens:
            return True
        # A single shared word (for example, "business" in a team bio) is
        # not enough to make a generic page relevant to a multiword target.
        if len(role_tokens) > 1 and role_tokens <= result_tokens:
            return True
    return False


def _is_too_senior(title: str, experience_level: str | None) -> bool:
    return experience_level in {"internship", "entry_level"} and bool(
        SENIOR_TITLE_PATTERN.search(title)
    )


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
    at_match = re.match(r"^.{3,100}?\s+at\s+(.+)$", name, flags=re.I)
    if at_match:
        name = at_match.group(1)
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
    if host == "jobs.citi.com":
        return "https://jobs.citi.com/search-jobs"
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


def discover_company_sites(
    role_keywords: list[str], locations: list[str], experience_level: str | None = None
) -> list[dict[str, str]]:
    """Search through the local SearXNG instance using saved role/location terms."""
    search_url = f"{settings.searxng_url.rstrip('/')}{SEARXNG_SEARCH_PATH}"
    found: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Accept": "application/json"},
        ) as client:
            for query in _query_groups(role_keywords, locations, experience_level):
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
                    if NOISE_TITLE_PATTERN.search(title):
                        continue
                    if is_excluded_company_url(url):
                        continue
                    if not _is_career_destination(url):
                        continue
                    signals = f"{title} {url} {description}".casefold()
                    if _is_too_senior(title, experience_level):
                        continue
                    if not _role_relevant(signals, role_keywords):
                        continue
                    # Never present search snippets (including AI-generated
                    # summaries) as verified job details.
                    description = ""
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
