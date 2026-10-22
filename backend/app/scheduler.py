"""Background company scans for the single local API instance."""

import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import CareerSite, NotificationSettings, SearchPreferences
from app.scans import run_scheduled_company_discovery, scan_company

logger = logging.getLogger(__name__)


def run_scheduled_scans() -> None:
    """Scan saved companies one at a time and persist any new job alerts."""
    run_scheduled_company_discovery()
    with SessionLocal() as db:
        preferences = db.get(SearchPreferences, 1)
        if preferences is None or not any(
            [preferences.role_keywords, preferences.locations, preferences.skills,
             preferences.work_arrangements]
        ):
            return
        company_ids = list(db.scalars(select(CareerSite.id).order_by(CareerSite.id)).all())

    for company_id in company_ids:
        try:
            with SessionLocal() as db:
                result = scan_company(company_id, db)
                logger.info(
                    "Scheduled scan complete for %s: %s new, %s alerts, %s skipped",
                    result.company, result.new_count, result.notified_count, result.skipped_count,
                )
        except HTTPException as exc:
            logger.warning("Scheduled scan skipped for company %s: %s", company_id, exc.detail)
        except Exception:
            logger.exception("Scheduled scan failed for company %s", company_id)


async def scheduled_scan_loop() -> None:
    """Run an initial scan shortly after startup, then scan at a steady interval."""
    await asyncio.sleep(10)
    while True:
        cycle_started = asyncio.get_running_loop().time()
        try:
            await asyncio.to_thread(run_scheduled_scans)
        except Exception:
            logger.exception("Scheduled company scan cycle failed")
        while True:
            with SessionLocal() as db:
                saved_settings = db.get(NotificationSettings, 1)
                interval_minutes = (
                    saved_settings.scan_interval_minutes if saved_settings
                    else settings.scan_interval_minutes
                )
            wait_seconds = interval_minutes * 60 - (
                asyncio.get_running_loop().time() - cycle_started
            )
            if wait_seconds <= 0:
                break
            await asyncio.sleep(min(60, wait_seconds))
