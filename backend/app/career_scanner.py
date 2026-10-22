"""Conservative scanner for user-selected public company career pages."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

BOT_NAME = "JobHarborJobFinder"
USER_AGENT = "JobHarborJobFinder/0.1 (user-directed career-page scanner)"
MAX_PAGE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 512_000
MAX_JOB_PAGES = 10
MAX_CRAWL_DELAY = 30
DEFAULT_CRAWL_DELAY = 1
JOB_PATH_HINTS = (
    "job", "career", "opening", "position", "requisition", "vacanc", "role", "posting"
)


class ScanError(ValueError):
    """A scan cannot safely or successfully continue."""


@dataclass
class Candidate:
    url: str
    title: str
    description: str
    posted_at: datetime | None = None


@dataclass
class ScannedPosting:
    url: str
    title: str
    description: str
    score: int
    matched_terms: list[str]
    posted_at: datetime | None = None


def _parse_posted_at(value: object) -> datetime | None:
    """Parse an ISO-style datePosted field from structured career-site data."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    try:
        posted_at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at.astimezone(timezone.utc)


def validate_public_url(url: str) -> str:
    """Allow only public HTTP(S) hosts to prevent local-network requests."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ScanError("Use a public company careers URL that starts with http:// or https://.")
    if parsed.username or parsed.password:
        raise ScanError("URLs containing a username or password are not supported.")
    expected_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or expected_port
    except ValueError as exc:
        raise ScanError("The careers URL has an invalid port.") from exc
    if port != expected_port:
        raise ScanError("Only standard public web ports (80 and 443) are supported.")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ScanError("Local and private network addresses cannot be scanned.")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            ]
        except OSError as exc:
            raise ScanError("The careers website hostname could not be resolved.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ScanError("Local and private network addresses cannot be scanned.")
    return url


def _same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (
        a.scheme.lower() == b.scheme.lower()
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and (a.port or (443 if a.scheme == "https" else 80))
        == (b.port or (443 if b.scheme == "https" else 80))
    )


def _read_response(client: httpx.Client, url: str, byte_limit: int) -> tuple[int, str, str]:
    validate_public_url(url)
    try:
        with client.stream("GET", url) as response:
            status = response.status_code
            content_type = response.headers.get("content-type", "").lower()
            if status != 200:
                return status, content_type, ""
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > byte_limit:
                    raise ScanError("A page exceeded the scanner's size limit and was skipped.")
                chunks.append(chunk)
            body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            return status, content_type, body
    except httpx.HTTPError as exc:
        raise ScanError("The website could not be reached. Check the URL and try again later.") from exc


def _robots(client: httpx.Client, site_url: str) -> tuple[RobotFileParser, float]:
    parsed = urlsplit(site_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    status, content_type, body = _read_response(client, robots_url, MAX_ROBOTS_BYTES)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    if status == 404:
        parser.parse(["User-agent: *", "Allow: /"])
        return parser, DEFAULT_CRAWL_DELAY
    if status != 200:
        raise ScanError("The site's robots.txt could not be read; this scan was stopped.")
    if "html" in content_type:
        raise ScanError("The site's robots.txt returned an HTML page; this scan was stopped.")
    parser.parse(body.splitlines())
    delay = parser.crawl_delay(BOT_NAME) or parser.crawl_delay("*") or DEFAULT_CRAWL_DELAY
    if delay > MAX_CRAWL_DELAY:
        raise ScanError(f"The site requests a {delay}-second crawl delay; scan skipped.")
    return parser, max(float(delay), DEFAULT_CRAWL_DELAY)


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        node.decompose()
    # Keep worst-case UTF-8 size below MySQL's 64 KiB TEXT limit.
    return " ".join(soup.stripped_strings)[:12_000]


def _listing_candidates(html: str, page_url: str) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[Candidate] = []
    by_url: dict[str, Candidate] = {}
    for anchor in soup.find_all("a", href=True):
        raw_href = unescape(str(anchor.get("href", "")).strip())
        try:
            absolute_url = urljoin(page_url, raw_href)
            parsed = urlsplit(absolute_url)
            same_origin = _same_origin(page_url, absolute_url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not same_origin:
            continue
        if absolute_url in by_url or absolute_url.split("#", 1)[0] == page_url.split("#", 1)[0]:
            continue
        title = " ".join(anchor.stripped_strings).strip()
        if len(title) < 4 or len(title) > 255:
            continue
        if title.casefold() in {"apply", "apply now", "jobs", "careers", "view all jobs", "see all jobs"}:
            continue
        classes: list[str] = list(anchor.get("class", []))
        for ancestor in list(anchor.parents)[:4]:
            classes.extend(ancestor.get("class", []))
            classes.append(str(ancestor.get("id", "")))
        path_and_label = f"{parsed.path} {title} {' '.join(classes)}".lower()
        if not any(hint in path_and_label for hint in JOB_PATH_HINTS):
            continue
        ancestor = anchor.find_parent(["article", "li"])
        if ancestor is None:
            ancestor = anchor.parent
        description = " ".join(ancestor.stripped_strings) if ancestor else title
        description = description[:4000]
        candidate = Candidate(absolute_url, title, description)
        candidates.append(candidate)
        by_url[absolute_url] = candidate
        if len(candidates) >= 100:
            break

    # Many career sites render their visible listings from JSON-LD or a JSON
    # hydration payload. Read those data blocks without executing site scripts.
    structured: list[object] = []
    for script in soup.find_all("script"):
        script_type = str(script.get("type", "")).lower()
        script_id = str(script.get("id", "")).lower()
        text = script.string or script.get_text()
        text = text.strip()
        if not text:
            continue
        payload: object | None = None
        if "ld+json" in script_type or script_type == "application/json" or script_id in {
            "__next_data__", "__nuxt_data__", "__initial_state__", "__apollo_state__"
        }:
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError, RecursionError):
                pass
        elif re.match(r"\s*(?:window\.)?__(?:INITIAL_STATE|NEXT_DATA)__\s*=", text, re.I):
            # Some apps put valid JSON in a simple JavaScript assignment.
            json_start = text.find("{")
            if json_start >= 0:
                try:
                    payload, _ = json.JSONDecoder().raw_decode(text[json_start:])
                except (json.JSONDecodeError, RecursionError):
                    pass
        if payload is not None:
            structured.append(payload)

    def walk(value: object):
        pending = [value]
        inspected = 0
        while pending and inspected < 20_000:
            current = pending.pop()
            inspected += 1
            if isinstance(current, dict):
                yield current
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)

    for payload in structured:
        for item in walk(payload):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or item.get("jobTitle")
            raw_url = (
                item.get("url") or item.get("jobUrl") or item.get("absolute_url")
                or item.get("absoluteUrl") or item.get("applyUrl")
            )
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("@id") or raw_url.get("url")
            if not isinstance(title, str) or not isinstance(raw_url, str):
                continue
            title = " ".join(title.split())
            if len(title) < 4 or len(title) > 255:
                continue
            try:
                absolute_url = urljoin(page_url, raw_url)
                parsed = urlsplit(absolute_url)
                if parsed.scheme not in {"http", "https"} or not _same_origin(page_url, absolute_url):
                    continue
            except ValueError:
                continue
            posted_at = _parse_posted_at(item.get("datePosted"))
            description = item.get("description", "")
            if not isinstance(description, str):
                description = ""
            description = " ".join(BeautifulSoup(description, "html.parser").stripped_strings)
            if absolute_url in by_url:
                existing = by_url[absolute_url]
                if existing.posted_at is None:
                    existing.posted_at = posted_at
                if len(description) > len(existing.description):
                    existing.description = description[:4000]
                continue
            if absolute_url.split("#", 1)[0] == page_url.split("#", 1)[0]:
                continue
            path_and_label = f"{parsed.path} {title}".lower()
            if not any(hint in path_and_label for hint in JOB_PATH_HINTS):
                continue
            candidate = Candidate(absolute_url, title, description[:4000] or title, posted_at)
            candidates.append(candidate)
            by_url[absolute_url] = candidate
            if len(candidates) >= 200:
                break
    candidates.sort(
        key=lambda candidate: (
            candidate.posted_at is not None,
            candidate.posted_at or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return candidates[:MAX_JOB_PAGES]


def _score_posting(text: str, preferences: dict) -> tuple[int, list[str]]:
    searchable = " ".join(text.casefold().split())
    groups = [
        (preferences.get("role_keywords", []), 0.45),
        (preferences.get("locations", []), 0.20),
        (preferences.get("skills", []), 0.25),
        (preferences.get("work_arrangements", []), 0.10),
    ]
    configured_weight = sum(weight for terms, weight in groups if terms)
    if configured_weight == 0:
        raise ScanError("Add at least one role, location, skill, or work-style preference before scanning.")
    score = 0.0
    matched: list[str] = []
    for terms, weight in groups:
        cleaned = [str(term).casefold().strip() for term in terms if str(term).strip()]
        if not cleaned:
            continue
        hits = []
        for term in cleaned:
            variants = {
                "onsite": ("onsite", "on-site", "in office"),
                "remote": ("remote", "work from home", "distributed"),
                "hybrid": ("hybrid",),
            }.get(term, (term,))
            if any(variant in searchable for variant in variants):
                hits.append(term)
        score += weight * len(hits) / len(cleaned)
        matched.extend(hits)
    return round(100 * score / configured_weight), matched


def scan_career_site(site_url: str, preferences: dict) -> tuple[list[ScannedPosting], int, list[str]]:
    """Fetch one career page and a small set of allowed job detail pages."""
    validate_public_url(site_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
    }
    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
    ) as client:
        robots, delay = _robots(client, site_url)
        if not robots.can_fetch(BOT_NAME, site_url):
            raise ScanError("robots.txt disallows scanning this careers page.")
        time.sleep(delay)
        status, content_type, listing = _read_response(client, site_url, MAX_PAGE_BYTES)
        if status != 200 or "html" not in content_type:
            raise ScanError("The careers page did not return a readable HTML page.")
        candidates = _listing_candidates(listing, site_url)
        if not candidates:
            return [], 0, ["No same-site job links were found. Try the company's direct careers or ATS page."]

        postings: list[ScannedPosting] = []
        skipped = 0
        errors: list[str] = []
        for candidate in candidates:
            if not robots.can_fetch(BOT_NAME, candidate.url):
                skipped += 1
                continue
            time.sleep(delay)
            try:
                status, content_type, detail_html = _read_response(
                    client, candidate.url, MAX_PAGE_BYTES
                )
                if status != 200 or "html" not in content_type:
                    skipped += 1
                    continue
                detail = _page_text(detail_html)
                description = detail if len(detail) >= 40 else candidate.description
                score, terms = _score_posting(
                    f"{candidate.title} {candidate.description} {description}", preferences
                )
                postings.append(
                    ScannedPosting(
                        candidate.url, candidate.title, description, score, terms,
                        candidate.posted_at,
                    )
                )
            except ScanError as exc:
                skipped += 1
                if len(errors) < 3:
                    errors.append(str(exc))
        return postings, skipped, errors
