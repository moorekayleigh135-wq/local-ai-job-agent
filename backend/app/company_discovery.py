"""Discover public employer career pages from user-approved search terms."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup

DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/html/"
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


def _decode_search_url(value: str) -> str:
    """Unwrap DuckDuckGo redirect links and return the destination URL."""
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.casefold().endswith("duckduckgo.com"):
        destination = parse_qs(parsed.query).get("uddg", [None])[0]
        if destination:
            return destination
    return value


def _search_results(html: str) -> list[dict[str, str]]:
    """Extract public web results from DuckDuckGo's lightweight HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    for result in soup.select(".result"):
        link = result.select_one("a.result__a")
        if link is None:
            continue
        url = link.get("href")
        title = link.get_text(" ", strip=True)
        if not isinstance(url, str) or not title:
            continue
        snippet = result.select_one(".result__snippet")
        results.append({
            "url": _decode_search_url(url),
            "title": title,
            "description": snippet.get_text(" ", strip=True) if snippet else "",
        })
    return results


def discover_company_sites(role_keywords: list[str], locations: list[str]) -> list[dict[str, str]]:
    """Search the public web using only the explicitly selected role/location terms."""
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
        "User-Agent": "JobHarbor local job discovery/1.0",
    }
    found: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), headers=headers) as client:
            for query in _query_groups(role_keywords, locations):
                response = client.get(DUCKDUCKGO_SEARCH_URL, params={"q": query, "kl": "us-en"})
                if response.status_code in {403, 429}:
                    raise CompanyDiscoveryError(
                        "DuckDuckGo temporarily limited automated searches. Discovery will retry later."
                    )
                response.raise_for_status()
                results = _search_results(response.text)
                if not results and any(marker in response.text.casefold() for marker in (
                    "anomaly-modal", "captcha", "unusual traffic", "automated requests"
                )):
                    raise CompanyDiscoveryError(
                        "DuckDuckGo presented an automated-traffic check. Discovery will retry later."
                    )
                for item in results:
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
    except httpx.HTTPStatusError as exc:
        raise CompanyDiscoveryError(
            f"DuckDuckGo search returned HTTP {exc.response.status_code}. Discovery will retry later."
        ) from exc
    except httpx.TimeoutException as exc:
        raise CompanyDiscoveryError("DuckDuckGo search timed out. Discovery will retry later.") from exc
    except httpx.HTTPError as exc:
        raise CompanyDiscoveryError(
            "DuckDuckGo could not be reached because of a network or TLS connection error."
        ) from exc
    except (ValueError, TypeError) as exc:
        raise CompanyDiscoveryError("DuckDuckGo returned an unreadable response.") from exc
    return list(found.values())
