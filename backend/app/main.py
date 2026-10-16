"""FastAPI entry point for the Local AI Job Agent."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from app import models  # Register SQLAlchemy models before create_all().
from app.config import settings
from app.database import Base, check_database_connection, engine
from app.jobs import router as jobs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables when the API starts."""
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Local AI Job Agent API",
    description="Private API for a local-first AI job-search assistant.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(jobs_router)


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {
        "name": "Local AI Job Agent API",
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
    return {
        "status": "healthy",
        "service": "ollama",
        "model": settings.ollama_model,
    }
