"""Company career-page and search-preference endpoints."""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.career_scanner import ScanError, scan_career_site
from app.company_discovery import CompanyDiscoveryError, discover_company_sites
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import (
    BrowserPushSubscription, CareerSite, CompanyDiscoveryState,
    DiscoveredCareerSite, Job, JobNotification, JobScanMatch,
    NotificationSettings, SearchPreferences,
)
from app.push_notifications import endpoint_digest, push_is_configured, send_job_alert
from app.schemas import (
    BrowserPushConfig,
    BrowserPushSubscriptionCreate,
    BrowserPushSubscriptionSaved,
    CareerSiteCreate,
    CareerSiteRead,
    CompanyDiscoveryRead,
    DiscoveredCareerSiteRead,
    ScanSummary,
    JobNotificationRead,
    ScannedJobRead,
    SearchPreferencesRead,
    SearchPreferencesUpdate,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    MatchThresholdUpdate,
)

router = APIRouter(prefix="/scanner", tags=["company scanner"])
DatabaseSession = Annotated[Session, Depends(get_db)]


def _discovery_response(db: Session, message: str | None = None) -> CompanyDiscoveryRead:
    state = db.get(CompanyDiscoveryState, 1)
    suggestions = db.scalars(
        select(DiscoveredCareerSite)
        .where(DiscoveredCareerSite.dismissed.is_(False))
        .order_by(DiscoveredCareerSite.discovered_at.desc())
        .limit(50)
    ).all()
    return CompanyDiscoveryRead(
        enabled=bool(settings.brave_search_api_key),
        interval_hours=settings.company_discovery_interval_hours,
        last_run_at=state.last_run_at if state else None,
        message=message,
        last_error=state.last_error if state else None,
        suggestions=[DiscoveredCareerSiteRead.model_validate(item, from_attributes=True) for item in suggestions],
    )


def _run_company_discovery(db: Session) -> CompanyDiscoveryRead:
    if not settings.brave_search_api_key:
        raise HTTPException(
            status_code=503,
            detail="Company discovery is not configured. Add BRAVE_SEARCH_API_KEY to your .env file.",
        )
    preferences = db.get(SearchPreferences, 1)
    if preferences is None or not preferences.role_keywords:
        raise HTTPException(status_code=400, detail="Save at least one target role before discovering companies.")

    state = db.get(CompanyDiscoveryState, 1)
    if state is None:
        state = CompanyDiscoveryState(id=1)
        db.add(state)
    state.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    state.last_error = None
    db.commit()

    try:
        results = discover_company_sites(preferences.role_keywords, preferences.locations)
    except CompanyDiscoveryError as exc:
        state.last_error = str(exc)
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    watched_urls = {
        site.url.rstrip("/").casefold()
        for site in db.scalars(select(CareerSite)).all()
    }
    for item in results:
        url = item["url"].rstrip("/")
        if url.casefold() in watched_urls:
            continue
        candidate = db.scalar(select(DiscoveredCareerSite).where(DiscoveredCareerSite.url == url))
        if candidate is None:
            candidate = DiscoveredCareerSite(url=url, name=item["name"], description=item["description"])
            db.add(candidate)
        else:
            if candidate.dismissed:
                continue
            candidate.name = item["name"]
            candidate.description = item["description"]
            candidate.discovered_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        delete(DiscoveredCareerSite).where(
            DiscoveredCareerSite.discovered_at
            < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        )
    )
    db.commit()
    return _discovery_response(db, "Company suggestions refreshed.")


def run_scheduled_company_discovery() -> None:
    """Refresh employer suggestions once per configured interval."""
    if not settings.brave_search_api_key:
        return
    with SessionLocal() as db:
        preferences = db.get(SearchPreferences, 1)
        if preferences is None or not preferences.role_keywords:
            return
        state = db.get(CompanyDiscoveryState, 1)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if state is not None and state.last_run_at is not None:
            elapsed = now - state.last_run_at
            # Failed provider calls get a short retry window; successful searches
            # use the configured discovery interval to keep API usage predictable.
            interval_hours = 1 if state.last_error else settings.company_discovery_interval_hours
            if elapsed.total_seconds() < interval_hours * 3600:
                return
        try:
            _run_company_discovery(db)
        except HTTPException as exc:
            if exc.status_code == 503:
                return
            raise


@router.get("/status")
def scanner_status(db: DatabaseSession) -> dict[str, int | bool]:
    saved = db.get(NotificationSettings, 1)
    interval = saved.scan_interval_minutes if saved else settings.scan_interval_minutes
    return {"automatic_scans": True, "scan_interval_minutes": interval}


@router.get("/push-config", response_model=BrowserPushConfig)
def push_config() -> BrowserPushConfig:
    configured = push_is_configured()
    return BrowserPushConfig(
        enabled=configured,
        public_key=settings.vapid_public_key if configured else None,
    )


@router.post(
    "/push-subscriptions",
    response_model=BrowserPushSubscriptionSaved,
    status_code=status.HTTP_201_CREATED,
)
def save_push_subscription(
    payload: BrowserPushSubscriptionCreate,
    db: DatabaseSession,
) -> BrowserPushSubscriptionSaved:
    if not push_is_configured():
        raise HTTPException(status_code=503, detail="Web Push is not configured on this installation.")
    endpoint = str(payload.endpoint)
    if not endpoint.startswith("https://"):
        raise HTTPException(status_code=400, detail="Browser push endpoints must use HTTPS.")
    try:
        from app.career_scanner import validate_public_url

        validate_public_url(endpoint)
    except ScanError as exc:
        raise HTTPException(status_code=400, detail="The browser supplied an invalid push endpoint.") from exc
    digest = endpoint_digest(endpoint)
    row = db.scalar(
        select(BrowserPushSubscription).where(
            BrowserPushSubscription.endpoint_hash == digest
        )
    )
    if row is None:
        row = BrowserPushSubscription(endpoint_hash=digest, subscription_data={})
        db.add(row)
    row.subscription_data = payload.model_dump(mode="json")
    db.commit()
    return BrowserPushSubscriptionSaved(subscription_id=digest)


@router.delete("/push-subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_push_subscription(subscription_id: str, db: DatabaseSession) -> Response:
    if len(subscription_id) != 64 or any(char not in "0123456789abcdef" for char in subscription_id):
        raise HTTPException(status_code=404, detail="Push subscription not found.")
    row = db.scalar(
        select(BrowserPushSubscription).where(
            BrowserPushSubscription.endpoint_hash == subscription_id
        )
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def recommendation_for_score(score: int) -> str:
    """Translate keyword coverage into a plain-language review tier."""
    if score >= 80:
        return "Strong match"
    if score >= 60:
        return "Good match"
    return "Potential match"


def recommendation_tip_for_score(score: int) -> str:
    if score >= 80:
        return "Prioritize this one: most of your selected profile signals are present."
    if score >= 60:
        return "Review the listing to confirm the role and location fit."
    return "Compare the requirements with your profile before deciding."


@router.get("/companies", response_model=list[CareerSiteRead])
def list_companies(db: DatabaseSession) -> list[CareerSite]:
    return list(db.scalars(select(CareerSite).order_by(CareerSite.name)).all())


@router.get("/discovery", response_model=CompanyDiscoveryRead)
def get_company_discovery(db: DatabaseSession) -> CompanyDiscoveryRead:
    message = None
    if not settings.brave_search_api_key:
        message = "Add BRAVE_SEARCH_API_KEY to .env to enable automatic company discovery."
    return _discovery_response(db, message)


@router.post("/discovery/run", response_model=CompanyDiscoveryRead)
def discover_companies_now(db: DatabaseSession) -> CompanyDiscoveryRead:
    return _run_company_discovery(db)


@router.post("/discovery/{suggestion_id}/monitor", response_model=CareerSiteRead)
def monitor_discovered_company(suggestion_id: int, db: DatabaseSession) -> CareerSiteRead:
    suggestion = db.get(DiscoveredCareerSite, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Company suggestion not found.")
    site = db.scalar(select(CareerSite).where(CareerSite.url == suggestion.url))
    if site is None:
        site = CareerSite(name=suggestion.name[:120], url=suggestion.url[:512])
        db.add(site)
    db.delete(suggestion)
    db.commit()
    db.refresh(site)
    return site


@router.delete("/discovery/{suggestion_id}", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_discovered_company(suggestion_id: int, db: DatabaseSession) -> Response:
    suggestion = db.get(DiscoveredCareerSite, suggestion_id)
    if suggestion is not None:
        suggestion.dismissed = True
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/companies", response_model=CareerSiteRead, status_code=status.HTTP_201_CREATED)
def add_company(payload: CareerSiteCreate, db: DatabaseSession) -> CareerSite:
    site = CareerSite(name=payload.name.strip(), url=str(payload.url))
    db.add(site)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That careers URL is already saved.") from exc
    db.refresh(site)
    return site


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_company(company_id: int, db: DatabaseSession) -> Response:
    site = db.get(CareerSite, company_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    db.delete(site)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=SearchPreferencesRead)
def get_preferences(db: DatabaseSession) -> SearchPreferencesRead:
    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        return SearchPreferencesRead()
    return SearchPreferencesRead.model_validate(preferences, from_attributes=True)


@router.put("/preferences", response_model=SearchPreferencesRead)
def save_preferences(
    payload: SearchPreferencesUpdate,
    db: DatabaseSession,
) -> SearchPreferencesRead:
    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        preferences = SearchPreferences(id=1)
        db.add(preferences)
    preferences.role_keywords = payload.role_keywords
    preferences.locations = payload.locations
    preferences.skills = payload.skills
    preferences.work_arrangements = payload.work_arrangements
    preferences.min_match_score = payload.min_match_score
    db.commit()
    db.refresh(preferences)
    return SearchPreferencesRead.model_validate(preferences, from_attributes=True)


@router.put("/preferences/threshold", response_model=SearchPreferencesRead)
def save_match_threshold(
    payload: MatchThresholdUpdate,
    db: DatabaseSession,
) -> SearchPreferencesRead:
    preferences = db.get(SearchPreferences, 1)
    if preferences is None:
        preferences = SearchPreferences(id=1)
        db.add(preferences)
    preferences.min_match_score = payload.min_match_score
    db.commit()
    db.refresh(preferences)
    return SearchPreferencesRead.model_validate(preferences, from_attributes=True)


@router.get("/settings", response_model=NotificationSettingsRead)
def get_notification_settings(db: DatabaseSession) -> NotificationSettingsRead:
    saved = db.get(NotificationSettings, 1)
    return NotificationSettingsRead(
        scan_interval_minutes=saved.scan_interval_minutes if saved else settings.scan_interval_minutes,
        max_notifications_per_day=saved.max_notifications_per_day if saved else 10,
    )


@router.put("/settings", response_model=NotificationSettingsRead)
def save_notification_settings(
    payload: NotificationSettingsUpdate,
    db: DatabaseSession,
) -> NotificationSettingsRead:
    saved = db.get(NotificationSettings, 1)
    if saved is None:
        saved = NotificationSettings(id=1)
        db.add(saved)
    saved.scan_interval_minutes = payload.scan_interval_minutes
    saved.max_notifications_per_day = payload.max_notifications_per_day
    db.commit()
    return NotificationSettingsRead.model_validate(saved, from_attributes=True)


@router.get("/matches", response_model=list[ScannedJobRead])
def list_matches(
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ScannedJobRead]:
    query = (
        select(JobScanMatch, Job)
        .join(Job, Job.id == JobScanMatch.job_id)
        .order_by(JobScanMatch.match_score.desc(), JobScanMatch.scanned_at.desc())
        .limit(limit)
    )
    preferences = db.get(SearchPreferences, 1)
    if preferences is not None:
        query = query.where(JobScanMatch.match_score >= preferences.min_match_score)
    rows = db.execute(query).all()
    return [
        ScannedJobRead(
            job=job,
            match_score=match.match_score,
            recommendation=recommendation_for_score(match.match_score),
            recommendation_tip=recommendation_tip_for_score(match.match_score),
            matched_terms=match.matched_terms,
            scanned_at=match.scanned_at,
        )
        for match, job in rows
    ]


@router.get("/notifications", response_model=list[JobNotificationRead])
def list_notifications(
    db: DatabaseSession,
    unread_only: bool = True,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[JobNotificationRead]:
    query = (
        select(JobNotification, Job, JobScanMatch)
        .join(Job, Job.id == JobNotification.job_id)
        .outerjoin(JobScanMatch, JobScanMatch.job_id == Job.id)
        .order_by(JobNotification.created_at.desc(), JobNotification.id.desc())
        .limit(limit)
    )
    if unread_only:
        query = query.where(JobNotification.read_at.is_(None))
    preferences = db.get(SearchPreferences, 1)
    if preferences is not None:
        query = query.where(JobNotification.match_score >= preferences.min_match_score)
    rows = db.execute(query).all()
    return [
        JobNotificationRead(
            id=notice.id,
            job_id=job.id,
            job_title=job.title,
            company=job.company,
            source_url=job.source_url,
            match_score=notice.match_score,
            posted_at=notice.posted_at,
            recommendation=recommendation_for_score(notice.match_score),
            recommendation_tip=recommendation_tip_for_score(notice.match_score),
            matched_terms=match.matched_terms if match else [],
            created_at=notice.created_at,
            read_at=notice.read_at,
        )
        for notice, job, match in rows
    ]


@router.post("/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_notification_read(notification_id: int, db: DatabaseSession) -> Response:
    notice = db.get(JobNotification, notification_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    if notice.read_at is None:
        notice.read_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/companies/{company_id}/scan", response_model=ScanSummary)
def scan_company(company_id: int, db: DatabaseSession) -> ScanSummary:
    site = db.get(CareerSite, company_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    preferences = db.get(SearchPreferences, 1)
    if preferences is None or not any(
        [preferences.role_keywords, preferences.locations, preferences.skills, preferences.work_arrangements]
    ):
        raise HTTPException(status_code=400, detail="Save at least one search preference before scanning.")
    preference_data = {
        "role_keywords": preferences.role_keywords,
        "locations": preferences.locations,
        "skills": preferences.skills,
        "work_arrangements": preferences.work_arrangements,
        "min_match_score": preferences.min_match_score,
    }
    try:
        postings, skipped_count, warnings = scan_career_site(site.url, preference_data)
    except ScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_count = 0
    updated_count = 0
    notified_count = 0
    matched_count = 0
    below_threshold_count = 0
    created_notifications: list[JobNotification] = []
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    saved_settings = db.get(NotificationSettings, 1)
    daily_limit = saved_settings.max_notifications_per_day if saved_settings else 10
    daily_count = db.scalar(
        select(func.count(JobNotification.id)).where(JobNotification.created_at >= day_start)
    ) or 0
    remaining_alerts = max(0, daily_limit - daily_count)
    freshness_cutoff = now - timedelta(hours=settings.new_job_window_hours)
    for posting in postings:
        qualifies = posting.score >= preferences.min_match_score
        if qualifies:
            matched_count += 1
        else:
            below_threshold_count += 1
        job = db.scalar(select(Job).where(Job.source_url == posting.url).limit(1))
        is_new_listing = job is None
        if job is None:
            job = Job(
                title=posting.title,
                company=site.name,
                source_url=posting.url,
                description=posting.description,
                skills=[],
                qualifications=[],
            )
            db.add(job)
            db.flush()
            new_count += 1
        else:
            job.title = posting.title
            job.company = site.name
            job.description = posting.description
            updated_count += 1

        match = db.scalar(select(JobScanMatch).where(JobScanMatch.job_id == job.id))
        previous_score = match.match_score if match is not None else None
        if match is None:
            db.add(
                JobScanMatch(
                    job_id=job.id,
                    match_score=posting.score,
                    matched_terms=posting.matched_terms,
                    scanned_at=now,
                )
            )
        else:
            match.match_score = posting.score
            match.matched_terms = posting.matched_terms
            match.scanned_at = now

        within_freshness_window = (
            posting.posted_at is None or freshness_cutoff <= posting.posted_at <= now
        )
        newly_qualifies = (
            previous_score is not None and previous_score < preferences.min_match_score
        )
        already_alerted = db.scalar(
            select(JobNotification.id).where(JobNotification.job_id == job.id).limit(1)
        ) is not None
        if (
            qualifies and within_freshness_window and not already_alerted
            and (is_new_listing or newly_qualifies)
            and remaining_alerts > 0
        ):
            notification = JobNotification(
                job_id=job.id,
                match_score=posting.score,
                posted_at=(
                    posting.posted_at.astimezone(timezone.utc).replace(tzinfo=None)
                    if posting.posted_at is not None else None
                ),
            )
            db.add(notification)
            created_notifications.append(notification)
            notified_count += 1
            remaining_alerts -= 1

    site.last_scanned_at = now
    db.commit()
    for notification in created_notifications:
        send_job_alert(notification.id)
    return ScanSummary(
        company=site.name,
        scanned_count=len(postings) + skipped_count,
        matched_count=matched_count,
        below_threshold_count=below_threshold_count,
        new_count=new_count,
        updated_count=updated_count,
        notified_count=notified_count,
        skipped_count=skipped_count,
        warnings=warnings,
    )
