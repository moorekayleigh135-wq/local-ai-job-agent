"""Conservative scanner for user-selected public company career pages."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.job_matcher import score_postings_locally

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
    external_id: str | None = None
    job_level: str | None = None
    location: str | None = None
    employment_type: str | None = None
    experience_compatibility: int | None = None


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


def _parse_valid_through(value: object) -> datetime | None:
    """Treat a date-only validThrough value as valid through that full day."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        parsed = _parse_posted_at(value)
        if parsed is not None:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return _parse_posted_at(value)


def _structured_job_postings(html: str) -> list[dict]:
    """Read structured job records from embedded JSON without running scripts."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []
    payloads: list[object] = []

    for script in soup.find_all("script"):
        script_type = str(script.get("type", "")).casefold()
        script_id = str(script.get("id", "")).casefold()
        text = (script.string or script.get_text()).strip()
        if not text:
            continue
        if "json" in script_type or script_id in {
            "__next_data__", "__nuxt_data__", "__initial_state__", "__apollo_state__"
        }:
            try:
                payloads.append(json.loads(text))
            except (json.JSONDecodeError, TypeError, RecursionError):
                pass
        elif re.match(
            r"\s*(?:window\.)?__(?:INITIAL_STATE|NEXT_DATA|APOLLO_STATE|PRELOADED_STATE)__\s*=",
            text,
            re.I,
        ):
            json_start = text.find("{")
            if json_start >= 0:
                try:
                    payload, _ = json.JSONDecoder().raw_decode(text[json_start:])
                    payloads.append(payload)
                except (json.JSONDecodeError, RecursionError):
                    pass

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

    for payload in payloads:
        for item in walk(payload):
            kind = item.get("@type", [])
            if isinstance(kind, str):
                kind = [kind]
            if isinstance(kind, list) and any(
                str(value).rsplit("/", 1)[-1] == "JobPosting" for value in kind
            ):
                records.append(item)
                continue
            title = (
                item.get("title") or item.get("jobTitle")
                or item.get("positionTitle") or item.get("jobPostingTitle")
            )
            posted = (
                item.get("datePosted") or item.get("postingDate")
                or item.get("postedDate") or item.get("datePublished")
                or item.get("postingDateTime")
            )
            description = (
                item.get("description") or item.get("jobDescription")
                or item.get("body") or item.get("jobPostingDescription")
                or item.get("jobDescriptionText")
            )
            identifier = (
                item.get("jobId") or item.get("jobID") or item.get("requisitionId")
                or item.get("requisitionID") or item.get("reqId") or item.get("id")
            )
            title = _flatten_text(title).strip()
            posted = _flatten_text(posted).strip()
            description = _flatten_text(description).strip()
            if title and posted and description:
                records.append({
                    "@type": "JobPosting", "title": title, "datePosted": posted,
                    "description": description, "identifier": identifier,
                    "validThrough": item.get("validThrough") or item.get("expirationDate"),
                    "jobLocation": item.get("jobLocation") or item.get("location"),
                    "employmentType": item.get("employmentType") or item.get("employment_type"),
                    "experienceRequirements": (
                        item.get("experienceRequirements") or item.get("experienceLevel")
                        or item.get("careerLevel") or item.get("jobLevel")
                    ),
                })

    if not records:
        # A few employer pages publish their job data as page metadata rather
        # than JSON-LD. The caller still requires a direct posting URL and date.
        title = soup.find("meta", attrs={"property": "og:title"})
        title_text = title.get("content", "") if title else ""
        if not title_text:
            heading = soup.find("h1")
            title_text = " ".join(heading.stripped_strings) if heading else ""
        date_value = None
        for attrs in (
            {"itemprop": "datePosted"},
            {"property": "article:published_time"},
            {"name": "datePosted"},
        ):
            date_node = soup.find("meta", attrs=attrs)
            if date_node and date_node.get("content"):
                date_value = date_node["content"]
                break
        if not date_value:
            time_node = soup.find("time", attrs={"datetime": True})
            if time_node:
                date_value = time_node.get("datetime")
        if title_text and date_value:
            records.append({
                "@type": "JobPosting", "title": title_text,
                "datePosted": date_value, "description": _page_text(html),
            })
    return records


def _external_job_id(url: str, record: dict | None = None) -> str | None:
    """Return a source-scoped ID such as careers.ibm.com:129654."""
    host = (urlsplit(url).hostname or "").casefold()
    if host == "ibm.com" or host.endswith(".ibm.com"):
        host = "ibm.com"
    query = parse_qs(urlsplit(url).query)
    value = next((
        query[key][0] for key in query
        if key.casefold() in {"jobid", "job_id", "requisitionid", "reqid"} and query[key]
    ), None)
    if value is None and record is not None:
        identifier = record.get("identifier") or record.get("jobId") or record.get("requisitionId")
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name")
        if isinstance(identifier, (str, int)):
            value = str(identifier)
    if value is None:
        return None
    value = re.sub(r"[^A-Za-z0-9._-]", "", str(value))[:100]
    return f"{host}:{value}" if value else None


def _is_direct_job_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        _external_job_id(url) is not None
        or bool(re.search(r"/jobdetail(?:/|$)", parsed.path, re.I))
    )


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    return ""


def _job_experience(record: dict, title: str, description: str) -> tuple[str | None, float | None]:
    raw_level = next((
        record.get(key) for key in (
            "experienceLevel", "careerLevel", "jobLevel", "seniority",
            "experienceRequirements", "occupationalExperienceRequirements",
        ) if record.get(key)
    ), None)
    level_evidence = " ".join((_flatten_text(raw_level), title)).casefold()
    explicit_page_level = re.search(
        r"experience\s+level\s*[:\-–]?\s*(internship|entry[- ]level|professional|senior|executive)",
        description[:6000],
        re.I,
    )
    if explicit_page_level:
        level_evidence += " " + explicit_page_level.group(1).casefold()
    for value in (record.get("experienceRequirements"), record.get("occupationalExperienceRequirements")):
        months = _months_of_experience(value)
        if months is not None:
            break
    else:
        months = None
    level = None
    for label, pattern in (
        ("Internship", r"\b(intern(ship)?|student|co[- ]op)\b"),
        ("Executive", r"\b(director|vice president|\bvp\b|executive|chief)\b"),
        ("Senior", r"\b(senior|staff|principal|lead)\b"),
        ("Entry level", r"\b(entry[- ]level|junior|early career|graduate|associate)\b"),
        ("Professional", r"\b(mid[- ]level|professional)\b"),
    ):
        if re.search(pattern, level_evidence):
            level = label
            break
    required = re.search(
        r"\b(\d{1,2})(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
        r"(?:['’]s)?\s*(?:of\s+)?(?:relevant\s+)?experience",
        " ".join((_flatten_text(raw_level), description[:6000])).casefold(),
    )
    required_years = None
    if months is not None:
        required_years = months / 12
    if required:
        value = required.group(1)
        if value and required_years is None:
            required_years = float(value)
    if level is None and required_years is not None:
        level = "Entry level" if required_years < 2 else "Professional" if required_years < 6 else "Senior"
    return level, required_years


def _job_location(record: dict) -> str | None:
    value = record.get("jobLocation") or record.get("location")
    text = " ".join(_flatten_text(value).split())
    return text[:255] or None


def _months_of_experience(value: object) -> float | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if str(key).casefold() == "monthsofexperience":
                    try:
                        return max(0.0, float(item))
                    except (TypeError, ValueError):
                        continue
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
    return None


def _experience_score(
    candidate_years: object,
    required_years: float | None,
    job_level: str | None,
    target_level: str | None = None,
) -> int | None:
    level_labels = {
        "internship": "Internship",
        "entry_level": "Entry level",
        "professional": "Professional",
        "senior": "Senior",
        "executive": "Executive",
    }
    if target_level in level_labels:
        target = level_labels[target_level]
        levels = {"Internship": 0, "Entry level": 1, "Professional": 2, "Senior": 3, "Executive": 4}
        level_score = (
            max(0, 100 - abs(levels[target] - levels[job_level]) * 25)
            if job_level in levels else None
        )
        years_score = None
        if (
            isinstance(candidate_years, (int, float))
            and not isinstance(candidate_years, bool)
            and required_years is not None
        ):
            gap = max(0.0, required_years - max(0.0, float(candidate_years)))
            years_score = max(0, round(100 - gap * 25))
        scores = [score for score in (level_score, years_score) if score is not None]
        return min(scores) if scores else None
    if not isinstance(candidate_years, (int, float)) or isinstance(candidate_years, bool):
        return None
    years = max(0.0, float(candidate_years))
    if required_years is not None:
        if years >= required_years:
            return 100
        gap = required_years - years
        return max(0, round(100 - gap * 20))
    candidate_level = "Entry level" if years < 2 else "Professional" if years < 6 else "Senior"
    levels = {"Internship": 0, "Entry level": 1, "Professional": 2, "Senior": 3, "Executive": 4}
    if job_level not in levels:
        return None
    gap = abs(levels[candidate_level] - levels[job_level])
    return max(0, 100 - gap * 25)


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


def _same_job_site(left: str, right: str) -> bool:
    """Allow IBM's official careers host to link to IBM's main careers host."""
    if _same_origin(left, right):
        return True
    a, b = urlsplit(left), urlsplit(right)
    if a.scheme.lower() != b.scheme.lower():
        return False
    return all(
        host == "ibm.com" or host.endswith(".ibm.com")
        for host in ((a.hostname or "").casefold(), (b.hostname or "").casefold())
    )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.casefold()}://{(parsed.hostname or '').casefold()}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


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


def _render_with_browser(url: str, site_url: str) -> str | None:
    """Ask the isolated renderer for a bounded public page DOM."""
    from app.config import settings

    try:
        response = httpx.post(
            f"{settings.js_renderer_url.rstrip('/')}/render",
            json={"url": url, "site_url": site_url},
            timeout=httpx.Timeout(22.0, connect=3.0),
        )
        response.raise_for_status()
        payload = response.json()
        html = payload.get("html") if isinstance(payload, dict) else None
        rendered_url = payload.get("url") if isinstance(payload, dict) else None
        if (
            isinstance(html, str)
            and len(html.encode("utf-8")) <= MAX_PAGE_BYTES
            and isinstance(rendered_url, str)
            and _same_job_site(site_url, rendered_url)
        ):
            return html
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return None


def _is_verification_challenge(html: str) -> bool:
    text = html.casefold()
    return any(marker in text for marker in (
        "javascript is disabled", "enable javascript", "verify that you're not a robot",
        "captcha", "checking your browser", "unusual traffic", "security check",
    ))


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
            same_origin = _same_job_site(page_url, absolute_url)
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
                if parsed.scheme not in {"http", "https"} or not _same_job_site(page_url, absolute_url):
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


ROLE_TOKEN_ALIASES = {
    "analyst": "analytics",
    "analysis": "analytics",
    "consultant": "consulting",
    "consultancy": "consulting",
    "strategist": "strategy",
    "manager": "management",
    "management": "management",
    "engineer": "engineering",
}
ROLE_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "the", "to"}


def _role_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return [ROLE_TOKEN_ALIASES.get(token, token) for token in tokens]


def _role_matches_title(role: str, title: str) -> bool:
    """Require the job title itself to identify a requested role family."""
    role_tokens = [token for token in _role_tokens(role) if token not in ROLE_STOP_WORDS]
    title_tokens = _role_tokens(title)
    if not role_tokens or not title_tokens:
        return False
    if " ".join(role_tokens) in " ".join(title_tokens):
        return True
    shared = set(role_tokens) & set(title_tokens)
    if len(role_tokens) == 1:
        return bool(shared)
    return len(shared) >= 2 and len(shared) / len(set(role_tokens)) >= 0.5


def _score_posting(
    title: str,
    text: str,
    preferences: dict,
    *,
    job_level: str | None = None,
    required_years: float | None = None,
    role_context: str = "",
) -> tuple[int, list[str]]:
    searchable = " ".join(text.casefold().split())
    roles = [str(term).strip() for term in preferences.get("role_keywords", []) if str(term).strip()]
    matched_roles = [role for role in roles if _role_matches_title(role, f"{title} {role_context}")]
    if roles and not matched_roles:
        return 0, []
    groups = [
        (roles, 0.45),
        (preferences.get("locations", []), 0.10),
        (preferences.get("skills", []), 0.30),
        (preferences.get("work_arrangements", []), 0.05),
    ]
    target_level = preferences.get("effective_experience_level") or preferences.get("experience_level")
    experience_score = _experience_score(
        preferences.get("candidate_years_experience"), required_years, job_level,
        target_level,
    )
    if experience_score is not None:
        groups.append((["experience"], 0.10))
    configured_weight = sum(weight for terms, weight in groups if terms)
    if configured_weight == 0:
        raise ScanError("Add at least one role, location, skill, or work-style preference before scanning.")
    score = 0.0
    matched: list[str] = []
    for group_index, (terms, weight) in enumerate(groups):
        cleaned = [str(term).casefold().strip() for term in terms if str(term).strip()]
        if not cleaned:
            continue
        if group_index == 0:
            # Roles are alternatives: match one requested title family instead
            # of penalizing a posting because the profile contains many roles.
            score += weight
            matched.extend(matched_roles)
            continue
        if group_index == 4:
            score += weight * experience_score / 100
            matched.append(
                f"experience level: {job_level or 'not listed'}"
                if target_level not in {None, "any"}
                else f"experience: {job_level or f'{required_years:g}+ years'}"
            )
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
        if group_index in {1, 3}:
            coverage = 1.0 if hits else 0.0
        elif group_index == 2:
            # Resume skills are alternatives, not a checklist every posting
            # must satisfy. Cap the denominator so a long resume skill list
            # does not suppress otherwise strong matches.
            coverage = min(1.0, len(hits) / min(5, len(cleaned)))
        else:
            coverage = len(hits) / len(cleaned)
        score += weight * coverage
        matched.extend(hits)
    return round(100 * score / configured_weight), matched


def scan_career_site(
    site_url: str, preferences: dict, freshness_hours: int = 720
) -> tuple[list[ScannedPosting], int, list[str]]:
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
        if _is_direct_job_url(site_url):
            candidates = [Candidate(site_url, "Job posting", "")]
        else:
            time.sleep(delay)
            status, content_type, listing = _read_response(client, site_url, MAX_PAGE_BYTES)
            if status == 404 or status == 410:
                raise ScanError("The monitored careers page returned 404 or 410. Update it to the employer's current careers URL.")
            if 300 <= status < 400:
                raise ScanError("The monitored careers page redirected. Add the destination careers URL directly.")
            if status != 200 or "html" not in content_type:
                raise ScanError("The careers page did not return a readable HTML page.")
            candidates = _listing_candidates(listing, site_url)
            listing_challenge = _is_verification_challenge(listing)
            listing_render_failed = False
            if not candidates and not listing_challenge:
                rendered_listing = _render_with_browser(site_url, site_url)
                if rendered_listing is None:
                    listing_render_failed = True
                elif _is_verification_challenge(rendered_listing):
                    listing_challenge = True
                else:
                    candidates = _listing_candidates(rendered_listing, site_url)
        if not candidates:
            warnings = ["No same-site job links were found. Try the company's direct careers or ATS page."]
            if not _is_direct_job_url(site_url) and listing_challenge:
                warnings.append("The listing is behind a JavaScript or bot-verification challenge; the scanner will not bypass it.")
            if not _is_direct_job_url(site_url) and listing_render_failed:
                warnings.append("The isolated JavaScript reader could not render the listing page.")
            return [], 0, warnings

        postings: list[ScannedPosting] = []
        skipped = 0
        unverified = 0
        not_found = 0
        forbidden = 0
        redirected = 0
        javascript_challenges = 0
        render_failures = 0
        rendered_pages = 0
        errors: list[str] = []
        robots_by_origin = {_origin(site_url): (robots, delay)}
        for candidate in candidates:
            candidate_origin = _origin(candidate.url)
            if candidate_origin not in robots_by_origin:
                try:
                    robots_by_origin[candidate_origin] = _robots(client, candidate.url)
                except ScanError as exc:
                    skipped += 1
                    if len(errors) < 3:
                        errors.append(str(exc))
                    continue
            candidate_robots, candidate_delay = robots_by_origin[candidate_origin]
            if not candidate_robots.can_fetch(BOT_NAME, candidate.url):
                skipped += 1
                continue
            time.sleep(candidate_delay)
            try:
                status, content_type, detail_html = _read_response(
                    client, candidate.url, MAX_PAGE_BYTES
                )
                if status != 200 or "html" not in content_type:
                    skipped += 1
                    if status == 404 or status == 410:
                        not_found += 1
                    elif status in {401, 403}:
                        forbidden += 1
                    elif 300 <= status < 400:
                        redirected += 1
                    continue
                job_records = _structured_job_postings(detail_html)
                if (
                    not job_records
                    and rendered_pages < 3
                    and not _is_verification_challenge(detail_html)
                ):
                    rendered_pages += 1
                    rendered_detail = _render_with_browser(candidate.url, site_url)
                    if rendered_detail is None:
                        render_failures += 1
                    elif _is_verification_challenge(rendered_detail):
                        javascript_challenges += 1
                    else:
                        detail_html = rendered_detail
                        job_records = _structured_job_postings(detail_html)
                verified = []
                verified_at = datetime.now(timezone.utc)
                cutoff = verified_at - timedelta(hours=freshness_hours)
                requested_id = _external_job_id(candidate.url)
                for record in job_records:
                    record_identifier = record.get("identifier")
                    if isinstance(record_identifier, dict):
                        record_identifier = record_identifier.get("value") or record_identifier.get("name")
                    if (
                        requested_id is not None
                        and record_identifier is not None
                        and not re.sub(r"[^A-Za-z0-9._-]", "", str(record_identifier)).endswith(
                            requested_id.rsplit(":", 1)[-1]
                        )
                    ):
                        continue
                    posted_at = _parse_posted_at(record.get("datePosted"))
                    valid_through = _parse_valid_through(record.get("validThrough"))
                    record_title = record.get("title") or record.get("name")
                    if (
                        posted_at is not None
                        and cutoff <= posted_at <= datetime.now(timezone.utc)
                        and (valid_through is None or valid_through >= verified_at)
                        and isinstance(record_title, str)
                        and record_title.strip()
                    ):
                        verified.append((record, posted_at))
                if not verified:
                    skipped += 1
                    unverified += 1
                    if _is_verification_challenge(detail_html):
                        javascript_challenges += 1
                    continue
                record, posted_at = verified[0]
                detail = _page_text(detail_html)
                description = detail if len(detail) >= 40 else candidate.description
                record_description = record.get("description")
                if isinstance(record_description, str):
                    record_description = " ".join(
                        BeautifulSoup(record_description, "html.parser").stripped_strings
                    )
                    if len(record_description) > len(description):
                        description = record_description[:12_000]
                title = " ".join(str(record.get("title") or record.get("name") or candidate.title).split())
                job_level, required_years = _job_experience(record, title, description)
                role_context = _flatten_text(record.get("occupationalCategory"))
                location = _job_location(record)
                employment_type = _flatten_text(record.get("employmentType"))[:100] or None
                description_for_match = " ".join((candidate.description, description, location or "", role_context))
                score, terms = _score_posting(
                    title, description_for_match, preferences,
                    job_level=job_level,
                    required_years=required_years,
                    role_context=role_context,
                )
                postings.append(
                    ScannedPosting(
                        candidate.url, title, description, score, terms, posted_at,
                        _external_job_id(candidate.url, record), job_level,
                        location, employment_type,
                        _experience_score(
                            preferences.get("candidate_years_experience"), required_years,
                            job_level, preferences.get("effective_experience_level")
                            or preferences.get("experience_level"),
                        ),
                    )
                )
            except ScanError as exc:
                skipped += 1
                if len(errors) < 3:
                    errors.append(str(exc))
        if unverified:
            errors.append(
                f"Skipped {unverified} listing(s): the actual posting page did not verify a JobPosting record dated within the last {freshness_hours} hours."
            )
        fits = score_postings_locally(postings, preferences)
        if fits is None:
            errors.append("Local AI scoring was unavailable; keyword-based scores were used for this scan.")
        else:
            for index, fit in fits.items():
                posting = postings[index]
                experience_fit = fit.experience_fit
                if posting.experience_compatibility is not None:
                    experience_fit = min(experience_fit, posting.experience_compatibility)
                posting.score = round(
                    fit.role_fit * 0.40
                    + fit.skills_fit * 0.30
                    + experience_fit * 0.20
                    + fit.preference_fit * 0.10
                )
                if posting.experience_compatibility is not None:
                    posting.score = min(posting.score, posting.experience_compatibility)
                posting.matched_terms = [
                    *fit.matched_evidence,
                    f"Role fit {fit.role_fit}%",
                    f"Experience fit {experience_fit}%",
                ]
        for posting in postings:
            if posting.experience_compatibility is not None:
                posting.score = min(posting.score, posting.experience_compatibility)
        if not_found:
            errors.append(
                f"{not_found} posting link(s) returned 404 or 410. The source may have removed or moved those jobs; only live source pages are kept."
            )
        if redirected:
            errors.append(
                f"{redirected} posting page(s) redirected and were skipped. Add the final destination URL to monitor."
            )
        if forbidden:
            errors.append(
                f"{forbidden} posting page(s) denied automated access (401/403) and were skipped."
            )
        if javascript_challenges:
            errors.append(
                f"{javascript_challenges} posting page(s) returned a JavaScript or bot-verification challenge. The scanner skips those pages rather than bypassing the site's protection."
            )
        if render_failures:
            errors.append(
                f"The isolated JavaScript reader could not render {render_failures} posting page(s). Check that the browser service is running; static scanning still works."
            )
        return postings, skipped, errors
