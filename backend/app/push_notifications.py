"""Opt-in, encrypted Web Push delivery for saved browser devices."""

import hashlib
import json
import logging
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from sqlalchemy import select
from pywebpush import WebPushException, webpush

from app.config import settings
from app.database import SessionLocal
from app.models import BrowserPushSubscription, Job, JobNotification, PushServerKey

logger = logging.getLogger(__name__)


def push_is_configured() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def ensure_vapid_keys() -> None:
    """Create a stable local key pair, encrypting the private half in MySQL."""
    if settings.vapid_public_key and settings.vapid_private_key:
        return
    if bool(settings.vapid_public_key) != bool(settings.vapid_private_key):
        raise RuntimeError("VAPID public and private keys must be configured together.")
    with SessionLocal() as db:
        stored = db.get(PushServerKey, 1)
        if stored is not None:
            settings.vapid_public_key = stored.public_key
            settings.vapid_private_key = stored.private_key["der_base64"]
            return
        private = ec.generate_private_key(ec.SECP256R1())
        public_numbers = private.public_key().public_numbers()
        public_bytes = (
            b"\x04"
            + public_numbers.x.to_bytes(32, "big")
            + public_numbers.y.to_bytes(32, "big")
        )
        public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
        private_der = private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        private_key = base64.b64encode(private_der).decode("ascii")
        db.add(PushServerKey(
            id=1,
            public_key=public_key,
            private_key={"der_base64": private_key},
        ))
        db.commit()
        settings.vapid_public_key = public_key
        settings.vapid_private_key = private_key


def send_job_alert(notification_id: int) -> None:
    """Send an encrypted push payload to each opted-in browser subscription."""
    if not push_is_configured():
        return
    with SessionLocal() as db:
        row = db.execute(
            select(JobNotification, Job).join(Job, Job.id == JobNotification.job_id)
            .where(JobNotification.id == notification_id)
        ).first()
        if row is None:
            return
        notice, job = row
        payload = json.dumps({
            "title": f"New job match · {notice.match_score}%",
            "body": f"{job.title or 'New opportunity'} · {job.company or 'Company'}",
            "url": job.source_url or "/notifications",
            "tag": f"jobharbor-alert-{notice.id}",
        })
        subscriptions = list(db.scalars(select(BrowserPushSubscription)).all())
        for subscription in subscriptions:
            try:
                # Push providers see delivery metadata, while pywebpush encrypts
                # the job alert payload for the subscribed browser.
                webpush(
                    subscription_info=subscription.subscription_data,
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_subject},
                    ttl=3600,
                    timeout=8,
                )
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in {404, 410}:
                    db.delete(subscription)
                    logger.info("Removed expired browser push subscription %s", subscription.id)
                else:
                    logger.warning("Browser push delivery failed for subscription %s", subscription.id)
            except Exception:
                logger.exception("Browser push delivery failed for subscription %s", subscription.id)
        db.commit()


def endpoint_digest(endpoint: str) -> str:
    """Return a non-reversible lookup key; the endpoint itself remains encrypted."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
