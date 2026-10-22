"""FastAPI entry point for JobHarbor, a local AI job-search assistant."""

from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import models  # Register SQLAlchemy models before create_all().
from app.config import settings
from app.auth import account_for_token, router as auth_router, COOKIE_NAME
from app.database import (
    Base, SessionLocal, check_database_connection, engine,
    migrate_legacy_personal_data, migrate_notification_posted_at,
)
from app.security import validate_encryption_key
from app.jobs import router as jobs_router
from app.profiles import router as profiles_router
from app.scans import router as scans_router
from app.scheduler import scheduled_scan_loop
from app.push_notifications import ensure_vapid_keys


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables when the API starts."""
    validate_encryption_key()
    Base.metadata.create_all(bind=engine)
    migrate_legacy_personal_data()
    migrate_notification_posted_at()
    ensure_vapid_keys()
    scan_task = asyncio.create_task(scheduled_scan_loop(), name="company-scan-scheduler")
    try:
        yield
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass


production = settings.app_env.casefold() == "production"
app = FastAPI(
    title="JobHarbor API",
    description="Private API for a local-first AI job-search assistant.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if production else "/docs",
    redoc_url=None if production else "/redoc",
    openapi_url=None if production else "/openapi.json",
)

PUBLIC_PATHS = {
    "/login",
    "/auth/status",
    "/auth/register",
    "/auth/login",
    "/health",
    "/health/db",
    "/health/ollama",
    "/api",
    "/service-worker.js",
    "/manifest.webmanifest",
    "/assets/jobharbor-icon.svg",
}
PRIVATE_UI_PATHS = {"/", "/account", "/notifications", "/settings", "/opportunities", "/assets/index.html", "/assets/profile.html", "/assets/scanner.html", "/assets/settings.html", "/assets/opportunities.html"}


@app.middleware("http")
async def local_security_headers(request: Request, call_next):
    """Add browser protections and reject cross-origin browser mutations."""
    response = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            try:
                parsed = urlsplit(source)
                expected_host = request.headers.get("host", "").casefold()
                same_origin = (
                    parsed.scheme == request.url.scheme
                    and parsed.netloc.casefold() == expected_host
                )
            except ValueError:
                same_origin = False
            if not same_origin:
                response = JSONResponse(
                    {"detail": "Cross-origin request rejected."}, status_code=403
                )

    if response is None and request.url.path not in PUBLIC_PATHS:
        with SessionLocal() as db:
            account = account_for_token(db, request.cookies.get(COOKIE_NAME))
        if account is None:
            if request.url.path in PRIVATE_UI_PATHS:
                response = RedirectResponse("/login", status_code=303)
            else:
                response = JSONResponse({"detail": "Sign in is required."}, status_code=401)
        else:
            request.state.account_id = account.id
            request.state.username = account.username

    if response is None:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'; form-action 'self'; "
        "base-uri 'self'; frame-ancestors 'none'; object-src 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
app.include_router(jobs_router)
app.include_router(scans_router)
app.include_router(profiles_router)
app.include_router(auth_router)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the project dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/account", include_in_schema=False)
def account_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "account.html")


@app.get("/notifications", include_in_schema=False)
def notifications_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "notifications.html")


@app.get("/settings", include_in_schema=False)
def settings_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/opportunities", include_in_schema=False)
def opportunities_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "opportunities.html")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/api", tags=["meta"])
def api_info() -> dict[str, str]:
    return {
        "name": "JobHarbor API",
        "status": "running",
        "environment": settings.app_env,
    }


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness endpoint used by Docker health checks."""
    return {"status": "healthy"}


@app.get("/health/db", tags=["health"])
def health_database() -> dict[str, str]:
    """Check MySQL connectivity without exposing credentials."""
    try:
        check_database_connection()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc
    return {"status": "healthy", "service": "mysql"}


@app.get("/health/ollama", tags=["health"])
def health_ollama() -> dict[str, str]:
    """Check whether Ollama is reachable."""
    try:
        response = httpx.get(
            f"{settings.ollama_url.rstrip('/')}/api/tags",
            timeout=3.0,
        )
        response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Ollama unavailable",
        ) from exc
    available_models = response.json().get("models", [])
    if not any(
        isinstance(model, dict) and model.get("name") == settings.ollama_model
        for model in available_models
    ):
        raise HTTPException(
            status_code=503,
            detail=f"Configured Ollama model '{settings.ollama_model}' is not downloaded.",
        )
    return {
        "status": "healthy",
        "service": "ollama",
        "model": settings.ollama_model,
    }
